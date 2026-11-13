"""Administration: user accounts, DeepSeek keys and cross-account inspection."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import current_admin
from app.logging_config import get_logger
from app.models import ChatSession, Node, User
from app.schemas import AdminProjectOut, UserCreateIn, UserUpdateIn, UserOut
from app.services import users as user_service

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])
CurrentAdmin = Depends(current_admin)


async def _stats(db: AsyncSession) -> dict[str, dict[str, int]]:
    """Per-owner counters for projects, files, chunks and chat sessions."""
    stats: dict[str, dict[str, int]] = {}

    def bucket(owner: uuid.UUID | None) -> dict[str, int]:
        key = str(owner) if owner else ""
        return stats.setdefault(key, {"projects": 0, "files": 0, "sessions": 0, "chunks": 0})

    node_rows = await db.execute(
        select(
            Node.owner_id,
            Node.kind,
            func.count(Node.id),
            func.coalesce(func.sum(Node.chunk_count), 0),
        ).group_by(Node.owner_id, Node.kind)
    )
    for owner_id, kind, count, chunks in node_rows.all():
        entry = bucket(owner_id)
        if kind == "project":
            entry["projects"] += int(count)
        elif kind == "file":
            entry["files"] += int(count)
            entry["chunks"] += int(chunks or 0)

    session_rows = await db.execute(
        select(ChatSession.owner_id, func.count(ChatSession.id)).group_by(ChatSession.owner_id)
    )
    for owner_id, count in session_rows.all():
        bucket(owner_id)["sessions"] += int(count)
    return stats


def _public(user: User, stats: dict[str, dict[str, int]]) -> UserOut:
    return UserOut.model_validate(
        user_service.user_to_dict(user, stats=stats.get(str(user.id), {}))
    )


@router.get("/users", response_model=list[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db), admin: User = CurrentAdmin
) -> list[UserOut]:
    stats = await _stats(db)
    return [_public(user, stats) for user in await user_service.list_users(db)]


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    payload: UserCreateIn, db: AsyncSession = Depends(get_db), admin: User = CurrentAdmin
) -> UserOut:
    user = await user_service.create_user(
        db,
        username=payload.username,
        password=payload.password,
        display_name=payload.display_name,
        role=payload.role,
        deepseek_api_key=payload.deepseek_api_key,
        deepseek_model=payload.deepseek_model,
        qwen_api_key=payload.qwen_api_key,
        qwen_model=payload.qwen_model,
    )

    await db.commit()
    logger.info("Admin '%s' created user '%s'", admin.username, user.username)
    return _public(user, {})


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateIn,
    db: AsyncSession = Depends(get_db),
    admin: User = CurrentAdmin,
) -> UserOut:
    user = await user_service.get_user(db, user_id)
    await user_service.update_user(
        db,
        user,
        display_name=payload.display_name,
        password=payload.password,
        role=payload.role,
        is_active=payload.is_active,
        deepseek_api_key=payload.deepseek_api_key,
        deepseek_model=payload.deepseek_model,
        clear_deepseek_key=payload.clear_deepseek_key,
        qwen_api_key=payload.qwen_api_key,
        qwen_model=payload.qwen_model,
        clear_qwen_key=payload.clear_qwen_key,
    )

    await db.commit()
    return _public(user, await _stats(db))


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: uuid.UUID, db: AsyncSession = Depends(get_db), admin: User = CurrentAdmin
) -> dict:
    user = await user_service.get_user(db, user_id)
    username = user.username
    removed = await user_service.delete_user(db, user, actor=admin)
    await db.commit()
    logger.info("Admin '%s' deleted user '%s'", admin.username, username)
    return {"deleted": True, "username": username, **removed}


@router.get("/projects", response_model=list[AdminProjectOut])
async def list_projects(
    owner_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    admin: User = CurrentAdmin,
) -> list[AdminProjectOut]:
    """Every project in the workspace, with its owner and size."""
    stmt = select(Node).where(Node.kind == "project").order_by(Node.created_at.desc())
    if owner_id is not None:
        stmt = stmt.where(Node.owner_id == owner_id)
    projects = list((await db.execute(stmt)).scalars().all())
    if not projects:
        return []

    owners = {
        str(row[0]): (row[2] or row[1])
        for row in (await db.execute(select(User.id, User.username, User.display_name))).all()
    }
    counters = await db.execute(
        select(
            Node.project_id,
            func.count(Node.id),
            func.coalesce(func.sum(Node.chunk_count), 0),
        )
        .where(Node.kind == "file", Node.project_id.in_([project.id for project in projects]))
        .group_by(Node.project_id)
    )
    by_project = {str(row[0]): (int(row[1]), int(row[2] or 0)) for row in counters.all()}
    output: list[AdminProjectOut] = []
    for project in projects:
        files, chunks = by_project.get(str(project.id), (0, 0))
        output.append(
            AdminProjectOut(
                id=project.id,
                name=project.name,
                owner_id=project.owner_id,
                owner=owners.get(str(project.owner_id)) if project.owner_id else None,
                files=files,
                chunks=chunks,
                created_at=project.created_at,
                updated_at=project.updated_at,
            )
        )
    return output


@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db), admin: User = CurrentAdmin) -> dict:
    stats = await _stats(db)
    totals = {"projects": 0, "files": 0, "sessions": 0, "chunks": 0}
    for entry in stats.values():
        for key in totals:
            totals[key] += entry.get(key, 0)
    users = await user_service.list_users(db)
    return {
        "users": len(users),
        "active_users": sum(1 for user in users if user.is_active),
        "admins": sum(1 for user in users if user.is_admin),
        "users_without_key": sum(1 for user in users if not user_service.has_any_personal_key(user)),
        **totals,
    }

