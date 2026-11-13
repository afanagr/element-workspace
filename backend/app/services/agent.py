"""Agent loop: streaming answers, RAG citations and workspace tool calling."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.factory import registry
from app.llm.schemas import (
    ChatMessage,
    NoProviderAvailable,
    ProviderError,
    StreamEnd,
    TextDelta,
    ToolCall,
    ToolCallEvent,
)
from app.logging_config import get_logger
from app.models import User
from app.services import chat_service
from app.services import nodes as node_service
from app.services.nodes import NodeError, build_tree, get_node, node_path
from app.services.parsing import parse_document
from app.services.rag import expand_node_ids, retrieve
from app.services.storage import storage
from app.services.tools import ToolContext, execute_tool, tool_specs

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are the AI assistant inside "{project}" - a single project of the private
"Element Workspace" document workspace.

Rules:
- The project "{project}" is your entire world: only its files exist. Never mention, read or
  write anything outside of it, and never invent node ids.
- You can inspect and modify the project files ONLY through the provided tools.
- Call list_workspace when you need to discover ids, then read_file / excel_read BEFORE editing.
- Spreadsheets: use excel_write (cells/rows) or excel_add_sheet; text documents: write_file.
- Delete/rename/move are allowed; the user can undo every action, so mention what you changed.
- When retrieved context is provided, cite it inline as [1], [2] - exactly matching the fragment numbers.
- Never claim a file was created, changed or deleted unless a tool call succeeded.
- Answer in the user's language, be concise, and show the important result (not raw dumps).
"""

# Tools that actually change the project. A final answer without at least one
# successful call from this set must not be trusted when the user asked for an
# edit - small models tend to claim success after only reading a file.
MUTATING_TOOLS = frozenset(
    {
        "write_file",
        "create_folder",
        "rename_node",
        "move_node",
        "delete_node",
        "excel_write",
        "excel_add_sheet",
        "translate_file",
        "create_pdf",
    }
)

_ACTION_RE = re.compile(
    r"\b(добавь|добавьте|запиши|запишите|измени|измените|создай|создайте|удали|"
    r"удалите|переименуй|переименуйте|перемести|переместите|обнови|обновите|вставь|"
    r"вставьте|заполни|заполните|дополни|дополните|переведи|переведите|add|append|"
    r"write|create|update|delete|remove|rename|move|insert|fill|edit|translate|put)\b",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(
    r"(^\s*(как|что|почему|зачем|где|когда|можно ли|how|what|why|where|can|is|are)\b)"
    r"|(\?\s*$)",
    re.IGNORECASE,
)
_FILE_HINT_RE = re.compile(
    r"(файл|документ|excel|xlsx|xls|word|docx|pdf|таблиц|лист|папк|проект|"
    r"file|document|sheet|folder|project|spreadsheet|txt|csv|json|markdown|log|"
    r"\.(txt|csv|md|json|ya?ml|log|py|js|ts|tsx|html|htm))",
    re.IGNORECASE,
)

MAX_TOOL_CORRECTIONS = 2
NO_MUTATION_REMINDER = (
    "You have NOT changed anything yet: no project-changing tool was called, so the "
    "file is untouched. Do NOT claim success. Do NOT call list/read tools again - the "
    "file contents are already in the tool results above. Call excel_write (for "
    "spreadsheets) or write_file (for text) RIGHT NOW with the final data, then wait "
    "for the tool result before answering."
)
NOT_APPLIED_NOTE = (
    "\n\n⚠️ Изменения не применены: модель не вызвала ни одного инструмента записи."
)


def _is_edit_request(text: str, has_attachments: bool = False) -> bool:
    if _QUESTION_RE.search(text):
        return False
    if not _ACTION_RE.search(text):
        return False
    return has_attachments or bool(_FILE_HINT_RE.search(text))


@dataclass(slots=True)
class AgentRequest:
    session_id: uuid.UUID
    text: str
    provider: str | None = None
    model: str | None = None
    attachments: list[str] = field(default_factory=list)
    use_rag: bool = True


def _render_tree(items: list[dict[str, Any]], depth: int = 0) -> list[str]:
    lines: list[str] = []
    for item in items:
        if len(lines) > 200:
            lines.append("… (truncated)")
            break
        marker = {"project": "[P]", "folder": "[D]", "file": "[F]"}.get(item["kind"], "[?]")
        lines.append(f"{'  ' * depth}{marker} {item['name']} (id={item['id']})")
        children = item.get("children") or []
        if children:
            lines.extend(_render_tree(children, depth + 1))
    return lines


async def _attachment_context(
    db: AsyncSession, node_ids: list[str], project_id: str
) -> tuple[str, list[dict[str, Any]]]:
    """Build the prompt block for attached files/folders/projects.

    Attachments outside the current project are ignored, so a client cannot widen
    the context by sending a foreign node id.
    """
    blocks: list[str] = []
    meta: list[dict[str, Any]] = []
    for raw in node_ids[:8]:
        try:
            node = await get_node(db, raw)
        except (NodeError, ValueError):
            continue
        in_project = str(node.project_id) == project_id or str(node.id) == project_id
        if not in_project:
            logger.warning("Ignoring attachment %s: outside the current project", raw)
            continue
        path = await node_path(db, node)
        meta.append({"id": str(node.id), "name": node.name, "kind": node.kind, "path": path})
        if node.kind == "file" and node.storage_key:
            try:
                data = await storage.read_bytes(node.storage_key)
                parsed = await anyio.to_thread.run_sync(parse_document, node.name, data)
                text = parsed.text[: settings.max_attachment_chars]
                blocks.append(f"### Attached file: {path} (id={node.id})\n{text}")
            except Exception as exc:  # noqa: BLE001
                blocks.append(f"### Attached file: {path} (id={node.id})\n(text extraction failed: {exc})")
        else:
            project_id = str(node.id) if node.kind == "project" else (
                str(node.project_id) if node.project_id else None
            )
            tree = await build_tree(db, project_id)
            body = "\n".join(_render_tree(tree))
            blocks.append(
                f"### Attached {node.kind}: {path} (id={node.id})\n{body[: settings.max_attachment_chars]}"
            )
    return "\n\n".join(blocks), meta


def _to_llm_history(records: list[Any]) -> list[ChatMessage]:
    history: list[ChatMessage] = []
    for record in records:
        if record.role not in ("user", "assistant"):
            continue
        content = record.content or ""
        # Never replay empty messages: progress rows (tool calls without text)
        # and failed/empty answers would be sent as "" and some providers
        # (e.g. YandexGPT 5.1) reject the whole request with "empty message text".
        if not content.strip():
            continue
        history.append(ChatMessage(role=record.role, content=content))
    return history


def _error_event(exc: ProviderError) -> dict[str, Any]:
    return {"type": "error", "code": exc.code or "provider_error", **exc.to_dict()}


async def run_agent(
    db: AsyncSession, request: AgentRequest, user: User
) -> AsyncIterator[dict[str, Any]]:
    """Stream agent events. The caller forwards them over WebSocket.

    The agent is bound to the chat session, the session is bound to a project and
    the project belongs to one account: retrieval and every tool call are limited
    to that project, so cross-project context is impossible by construction.
    """
    session = await chat_service.get_session(db, request.session_id, user)
    if session.project_id is None:
        yield {
            "type": "error",
            "code": "no_project",
            "message": "This chat is not bound to a project. Select a project and start a new chat.",
            "retryable": False,
        }
        return
    try:
        project = await node_service.get_project_for(db, session.project_id, user)
    except NodeError as exc:
        yield {
            "type": "error",
            "code": "no_project",
            "message": exc.message,
            "retryable": False,
        }
        return
    project_id = str(project.id)
    await db.commit()

    try:
        candidates = await registry.candidates(
            db, user, request.model, provider=request.provider
        )
    except NoProviderAvailable as exc:
        yield {
            "type": "error",
            "code": "no_provider",
            "message": exc.message,
            "details": exc.details,
            "retryable": False,
        }
        return

    candidate_index = 0
    resolved = candidates[0]

    attachment_text, attachment_meta = await _attachment_context(db, request.attachments, project_id)
    citations_payload: list[dict[str, Any]] = []
    context_block = ""
    if request.use_rag and request.text.strip():
        scope_ids = await expand_node_ids(db, request.attachments) if request.attachments else None
        retrieval = await retrieve(
            db,
            request.text,
            project_id=project_id,
            node_ids=scope_ids,
        )
        citations_payload = [citation.to_dict() for citation in retrieval.citations]
        context_block = retrieval.context_block()
        yield {"type": "citations", "items": citations_payload, "error": retrieval.error}

    user_content = request.text
    if attachment_text:
        user_content = (
            f"{request.text}\n\n---\nAttached context from the workspace "
            f"(use it directly, or use tools for more):\n\n{attachment_text}"
        )

    system_prompt = SYSTEM_PROMPT.replace("{project}", project.name)
    system_prompt += (
        f"\n\nCurrent project: \"{project.name}\" (id={project.id}). "
        "Only files inside this project exist for you; never reference other projects."
    )
    if context_block:
        system_prompt += (
            "\n\nRetrieved context from this project's index (cite as [n]):\n\n" + context_block
        )

    previous = await chat_service.history(db, session.id, limit=30)
    messages: list[ChatMessage] = [ChatMessage(role="system", content=system_prompt)]
    messages.extend(_to_llm_history(previous))
    messages.append(ChatMessage(role="user", content=user_content))

    user_record = await chat_service.add_message(
        db, session.id, role="user", content=request.text, attachments=attachment_meta
    )
    await chat_service.ensure_title(db, session, request.text)
    session.provider = resolved.provider_name
    session.model = resolved.model
    await db.commit()
    yield {"type": "user_saved", "message_id": str(user_record.id)}
    yield {
        "type": "start",
        "session_id": str(session.id),
        "provider": resolved.provider_name,
        "model": resolved.model,
        "fallback_from": resolved.fallback_from,
    }

    started_at = time.perf_counter()
    specs = tool_specs()
    edit_requested = _is_edit_request(request.text, bool(request.attachments))
    mutations_done = 0
    corrections = 0

    for _step in range(max(1, settings.max_agent_steps)):
        step_parts: list[str] = []
        stream_end: StreamEnd | None = None
        step_started = time.perf_counter()
        try:
            async for event in resolved.provider.chat_stream(
                messages, model=resolved.model, tools=specs, temperature=0.2
            ):
                if isinstance(event, TextDelta):
                    step_parts.append(event.text)
                    yield {"type": "delta", "text": event.text}
                elif isinstance(event, ToolCallEvent):
                    yield {
                        "type": "tool_call",
                        "id": event.call.id,
                        "name": event.call.name,
                        "arguments": event.call.arguments,
                        "status": "running",
                    }
                elif isinstance(event, StreamEnd):
                    stream_end = event
        except ProviderError as exc:
            duration_ms = (time.perf_counter() - step_started) * 1000
            await chat_service.log_llm_call(
                db,
                provider=resolved.provider_name,
                model=resolved.model,
                status="error",
                duration_ms=duration_ms,
                session_id=session.id,
                error=exc.message,
            )
            await db.commit()
            can_fallback = (
                not step_parts
                and candidate_index + 1 < len(candidates)
                and (exc.retryable or exc.is_auth_error or exc.is_rate_limit)
            )
            if can_fallback:
                failed = resolved.provider_name
                candidate_index += 1
                resolved = candidates[candidate_index]
                logger.warning(
                    "Provider %s failed (%s); switching to %s",
                    failed,
                    exc.message,
                    resolved.provider_name,
                )
                yield {
                    "type": "provider_fallback",
                    "from": failed,
                    "to": resolved.provider_name,
                    "reason": exc.message,
                }
                session.provider = resolved.provider_name
                session.model = resolved.model
                await db.commit()
                continue
            if step_parts:
                await chat_service.add_message(
                    db,
                    session.id,
                    role="assistant",
                    content="".join(step_parts),
                    provider=resolved.provider_name,
                    model=resolved.model,
                    error=exc.message,
                )
                await db.commit()
            yield _error_event(exc)
            return

        except Exception as exc:  # noqa: BLE001 - unexpected provider bug
            logger.exception("Agent stream crashed")
            await chat_service.add_message(
                db,
                session.id,
                role="assistant",
                content="".join(step_parts),
                provider=resolved.provider_name,
                model=resolved.model,
                error=str(exc),
            )
            await db.commit()
            yield {
                "type": "error",
                "code": "internal_error",
                "message": f"{type(exc).__name__}: {exc}",
                "retryable": False,
            }
            return

        if stream_end is None:
            yield {
                "type": "error",
                "code": "empty_response",
                "message": "The provider closed the stream without a response.",
                "provider": resolved.provider_name,
                "retryable": True,
            }
            return

        duration_ms = (time.perf_counter() - step_started) * 1000
        usage = stream_end.usage
        await chat_service.log_llm_call(
            db,
            provider=resolved.provider_name,
            model=resolved.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            duration_ms=duration_ms,
            session_id=session.id,
        )
        yield {
            "type": "usage",
            "provider": resolved.provider_name,
            "model": resolved.model,
            "usage": usage.as_dict(),
            "duration_ms": round(duration_ms, 1),
        }

        assistant = stream_end.message
        answer_text = assistant.content or ""
        tool_calls = assistant.tool_calls
        messages.append(
            ChatMessage(role="assistant", content=answer_text or None, tool_calls=tool_calls)
        )

        if not tool_calls:
            if edit_requested and mutations_done == 0:
                if corrections < MAX_TOOL_CORRECTIONS:
                    corrections += 1
                    logger.info(
                        "Model answered an edit request without calling a write tool; "
                        "asking again (%s/%s)",
                        corrections,
                        MAX_TOOL_CORRECTIONS,
                    )
                    messages.append(
                        ChatMessage(
                            role="user",
                            content="[AUTOMATIC CHECK] " + NO_MUTATION_REMINDER,
                        )
                    )
                    continue
                answer_text = f"{answer_text.rstrip()}{NOT_APPLIED_NOTE}"
            record = await chat_service.add_message(
                db,
                session.id,
                role="assistant",
                content=answer_text,
                provider=resolved.provider_name,
                model=resolved.model,
                citations=citations_payload,
                usage={
                    **usage.as_dict(),
                    "duration_ms": round(duration_ms, 1),
                    "total_duration_ms": round((time.perf_counter() - started_at) * 1000, 1),
                },
            )
            await db.commit()
            yield {
                "type": "assistant_done",
                "message_id": str(record.id),
                "content": answer_text,
                "citations": citations_payload,
            }
            return

        ctx = ToolContext(
            db=db,
            session_id=session.id,
            project_id=project_id,
            owner_id=str(user.id),
            user=user,
            is_admin=user.is_admin,
        )
        tool_payloads: list[dict[str, Any]] = []
        for call in tool_calls:
            result = await execute_tool(ctx, call.name, call.arguments)
            if result.ok and call.name in MUTATING_TOOLS:
                mutations_done += 1
            await db.commit()
            yield {
                "type": "tool_result",
                "id": call.id,
                "name": call.name,
                "ok": result.ok,
                "summary": result.summary[:4000],
                "operation_id": result.operation_id,
                "data": result.data,
            }
            tool_payloads.append(
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "ok": result.ok,
                    "summary": result.summary[:600],
                    "operation_id": result.operation_id,
                }
            )
            messages.append(
                ChatMessage(
                    role="tool",
                    tool_call_id=call.id,
                    name=call.name,
                    content=result.as_prompt()[:20000],
                )
            )
        record = await chat_service.add_message(
            db,
            session.id,
            role="assistant",
            content=answer_text,
            provider=resolved.provider_name,
            model=resolved.model,
            tool_calls=tool_payloads,
            usage=usage.as_dict(),
        )
        await db.commit()
        yield {
            "type": "assistant_progress",
            "message_id": str(record.id),
            "content": answer_text,
            "tool_calls": tool_payloads,
        }
        for change in ctx.changed_nodes:
            yield {"type": "node_changed", "id": change["id"], "event": change["event"]}
        ctx.changed_nodes.clear()

    yield {
        "type": "error",
        "code": "max_steps",
        "message": (
            f"Stopped after {settings.max_agent_steps} tool steps. "
            "Ask me to continue if the task is not finished."
        ),
        "retryable": False,
    }


