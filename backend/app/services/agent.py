"""Agent loop: streaming answers, RAG citations and workspace tool calling.

The prompt and the intent heuristics live in :mod:`app.services.agent_prompt`,
the run limits in :mod:`app.services.agent_budget`; this module only drives the
stream, the provider fallback and the tool calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
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
    ToolCallEvent,
)
from app.logging_config import get_logger
from app.models import User
from app.services import agent_prompt, chat_service
from app.services import nodes as node_service
from app.services.agent_budget import AgentBudget
from app.services.agent_prompt import (
    MAX_TOOL_CORRECTIONS,
    MUTATING_TOOLS,
    NO_MUTATION_REMINDER,
    NOT_APPLIED_NOTE,
    PDF_NOT_SPLIT_NOTE,
    PDF_SPLIT_REMINDER,
    SYSTEM_PROMPT,
    build_history,
    claims_file_action,
    is_edit_request,
    is_pdf_split_request,
    validate_citations,
)
from app.services.nodes import NodeError, build_tree, get_node, node_path
from app.services.parsing import parse_document
from app.services.rag import expand_node_ids, retrieve
from app.services.storage import storage
from app.services.tools import ToolContext, ToolResult, execute_tool, tool_specs

logger = get_logger(__name__)


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


def _error_event(exc: ProviderError) -> dict[str, Any]:
    return {"type": "error", "code": exc.code or "provider_error", **exc.to_dict()}


def _call_signature(name: str, arguments: dict[str, Any]) -> str:
    payload = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(f"{name}:{payload}".encode("utf-8")).hexdigest()


async def _call_tool(ctx: ToolContext, call: Any) -> ToolResult:
    """Execute one tool call with a hard timeout and JSON-parse error reporting."""
    if getattr(call, "parse_error", None):
        return ToolResult(
            False,
            f"{call.name}: arguments are not valid JSON ({call.parse_error}). "
            "Repeat the call with a valid JSON object.",
        )
    try:
        return await asyncio.wait_for(
            execute_tool(ctx, call.name, call.arguments),
            timeout=settings.tool_timeout_seconds,
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Tool %s timed out after %ss", call.name, settings.tool_timeout_seconds)
        return ToolResult(
            False,
            f"{call.name} timed out after {settings.tool_timeout_seconds:.0f}s. "
            "Try a smaller request or a different approach.",
        )


async def run_agent(
    db: AsyncSession,
    request: AgentRequest,
    user: User,
    *,
    cancel: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream agent events. The caller forwards them over WebSocket.

    The agent is bound to the chat session, the session is bound to a project and
    the project belongs to one account: retrieval and every tool call are limited
    to that project, so cross-project context is impossible by construction.
    """

    def cancelled() -> bool:
        return cancel is not None and cancel.is_set()

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

    # Retrieved/attached text is untrusted data, so it travels with the user turn
    # instead of the system prompt (prompt-injection hardening).
    blocks: list[str] = []
    if attachment_text:
        blocks.append("### ATTACHED CONTEXT (untrusted data)\n" + attachment_text)
    if context_block:
        blocks.append("### RETRIEVED CONTEXT (untrusted data, cite as [n])\n" + context_block)
    user_content = request.text
    if blocks:
        user_content = f"{request.text}\n\n---\n" + "\n\n".join(blocks)

    system_prompt = SYSTEM_PROMPT.replace("{project}", project.name)
    system_prompt += (
        f"\n\nCurrent project: \"{project.name}\" (id={project.id}). "
        "Only files inside this project exist for you; never reference other projects."
    )

    previous = await chat_service.history(db, session.id, limit=30)
    messages: list[ChatMessage] = [ChatMessage(role="system", content=system_prompt)]
    messages.extend(build_history(previous))
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
        "prompt_version": agent_prompt.PROMPT_VERSION,
    }

    budget = AgentBudget()
    specs = tool_specs()
    edit_requested = is_edit_request(request.text, bool(request.attachments))
    pdf_split_requested = is_pdf_split_request(request.text)
    mutations_done = 0
    pdf_split_done = False
    corrections = 0
    failed_calls: dict[str, int] = {}

    while budget.next_step():
        if cancelled():
            yield {"type": "cancelled", "message": "Stopped by the user."}
            return
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
        budget.charge(usage, extra_text="".join(step_parts))
        await chat_service.log_llm_call(
            db,
            provider=resolved.provider_name,
            model=resolved.model,
            kind="agent_step",
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            duration_ms=duration_ms,
            session_id=session.id,
        )
        assistant = stream_end.message
        answer_text = assistant.content or ""
        tool_calls = assistant.tool_calls
        logger.info(
            "Agent step %s/%s: %s tool call(s), %s tokens, %.0f ms, %.0fs left",
            budget.steps_used,
            budget.max_steps,
            len(tool_calls),
            usage.total_tokens,
            duration_ms,
            budget.seconds_left,
        )
        yield {
            "type": "usage",
            "provider": resolved.provider_name,
            "model": resolved.model,
            "usage": usage.as_dict(),
            "duration_ms": round(duration_ms, 1),
            "tokens_left": budget.tokens_left,
            "seconds_left": round(budget.seconds_left, 1),
        }
        messages.append(
            ChatMessage(role="assistant", content=answer_text or None, tool_calls=tool_calls)
        )

        if not tool_calls:
            if pdf_split_requested and not pdf_split_done:
                if corrections < MAX_TOOL_CORRECTIONS:
                    corrections += 1
                    logger.info(
                        "PDF split requested but pdf_split was not called; asking again (%s/%s)",
                        corrections,
                        MAX_TOOL_CORRECTIONS,
                    )
                    messages.append(
                        ChatMessage(
                            role="system",
                            content="[AUTOMATIC CHECK] " + PDF_SPLIT_REMINDER,
                        )
                    )
                    continue
                answer_text = f"{answer_text.rstrip()}{PDF_NOT_SPLIT_NOTE}"
            elif (
                edit_requested or claims_file_action(answer_text)
            ) and mutations_done == 0:
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
                            role="system",
                            content="[AUTOMATIC CHECK] " + NO_MUTATION_REMINDER,
                        )
                    )
                    continue
                answer_text = (
                    f"{NOT_APPLIED_NOTE.strip()}\n\n{answer_text}"
                    if answer_text.strip()
                    else NOT_APPLIED_NOTE.strip()
                )

            answer_text, citations_valid = validate_citations(answer_text, len(citations_payload))
            if citations_payload and not citations_valid:
                logger.info("Dropped citation markers that point to missing fragments")
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
                    "total_duration_ms": round((time.perf_counter() - budget.started) * 1000, 1),
                    "prompt_version": agent_prompt.PROMPT_VERSION,
                    "citations_valid": citations_valid,
                },
            )
            await db.commit()
            yield {
                "type": "assistant_done",
                "message_id": str(record.id),
                "content": answer_text,
                "citations": citations_payload,
                "citations_valid": citations_valid,
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
        stalled = False
        for call in tool_calls:
            result = await _call_tool(ctx, call)
            if result.ok and call.name == "pdf_split":
                pdf_split_done = True
            if result.ok and call.name in MUTATING_TOOLS:
                mutations_done += 1
            signature = _call_signature(call.name, call.arguments)
            if result.ok:
                failed_calls.pop(signature, None)
            else:
                failed_calls[signature] = failed_calls.get(signature, 0) + 1
                if failed_calls[signature] >= max(1, settings.tool_repeat_limit):
                    stalled = True
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
            if stalled:
                logger.warning(
                    "Tool %s failed %s time(s) with identical arguments; stopping the run",
                    call.name,
                    failed_calls[signature],
                )
                break

        record = await chat_service.add_message(
            db,
            session.id,
            role="assistant",
            content=answer_text,
            provider=resolved.provider_name,
            model=resolved.model,
            tool_calls=tool_payloads,
            usage={**usage.as_dict(), "duration_ms": round(duration_ms, 1)},
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

        if stalled:
            yield {
                "type": "error",
                "code": "stalled",
                "message": (
                    "The same tool call keeps failing with identical arguments. "
                    "Rephrase the request or give more details."
                ),
                "retryable": False,
            }
            return

    reason = "max_steps"
    if budget.seconds_left <= 0:
        reason = "deadline"
    elif budget.tokens_left <= 0:
        reason = "token_budget"
    labels = {
        "max_steps": f"Stopped after {budget.max_steps} tool steps.",
        "deadline": f"Stopped after {settings.agent_deadline_seconds:.0f}s: the time budget is exhausted.",
        "token_budget": "Stopped: the context budget of this request is exhausted.",
    }
    yield {
        "type": "error",
        "code": reason,
        "message": labels[reason] + " Ask me to continue if the task is not finished.",
        "retryable": False,
    }
