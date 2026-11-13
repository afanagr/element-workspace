"""Accounts, bearer sessions and per-account model connections.

Users are created by an administrator: there is no public sign-up. Passwords are
hashed with PBKDF2-HMAC-SHA256, tokens are random strings of which only the
SHA-256 digest is persisted, and connection API keys are encrypted with Fernet.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.logging_config import get_logger
from app.models import (
    AuthToken,
    ChatMessage,
    ChatSession,
    ModelConnection,
    Node,
    Operation,
    User,
    utcnow,
)
from app.security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    mask_secret,
    new_session_token,
    token_digest,
    verify_password,
)

logger = get_logger(__name__)

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,64}$")
_BASE_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
MIN_PASSWORD_LEN = 6
MAX_CONNECTIONS = 16
CONNECTION_NAME_LIMIT = 32
CONNECTION_MODEL_LIMIT = 128
CONNECTION_URL_LIMIT = 500


class UserError(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def normalize_username(value: str) -> str:
    username = (value or "").strip().lower()
    if not _USERNAME_RE.match(username):
        raise UserError(
            "Username must be 3-64 characters: letters, digits, dot, dash or underscore"
        )
    return username


def validate_password(value: str) -> str:
    password = value or ""
    if len(password) < MIN_PASSWORD_LEN:
        raise UserError(f"Password must be at least {MIN_PASSWORD_LEN} characters")
    return password


# ---------------------------------------------------------- model connections
def _clean_connection(item: dict[str, Any]) -> dict[str, Any]:
    name = str(item.get("name") or "").strip()
    base_url = str(item.get("base_url") or "").strip().rstrip("/")
    model = str(item.get("model") or "").strip()
    raw_key = item.get("api_key")
    api_key = raw_key.strip() if isinstance(raw_key, str) else ""

    if not name:
        raise UserError("Model name is required for every connection")
    if len(name) > CONNECTION_NAME_LIMIT:
        raise UserError(f"Model name must be at most {CONNECTION_NAME_LIMIT} characters")
    if not base_url or not _BASE_URL_RE.match(base_url):
        raise UserError(f"'{name}': address must start with http:// or https://")
    if len(base_url) > CONNECTION_URL_LIMIT:
        raise UserError(f"'{name}': address is too long")
    if not model:
        raise UserError(f"'{name}': model name is required")
    if len(model) > CONNECTION_MODEL_LIMIT:
        raise UserError(f"'{name}': model name is too long")

    return {
        "name": name,
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "clear_key": bool(item.get("clear_key")),
    }


def _clean_connections(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        connection = _clean_connection(dict(item))
        key = connection["name"].lower()
        if key in seen:
            raise UserError(f"Model name '{connection['name']}' is used more than once")
        seen.add(key)
        cleaned.append(connection)
    if len(cleaned) > MAX_CONNECTIONS:
        raise UserError(f"An account can have at most {MAX_CONNECTIONS} model connections")
    return cleaned


async def list_connections(db: AsyncSession, user: User | None) -> list[ModelConnection]:
    if user is None:
        return []
    result = await db.execute(
        select(ModelConnection)
        .where(ModelConnection.user_id == user.id)
        .order_by(ModelConnection.name)
    )
    return list(result.scalars().all())


async def connections_for_users(
    db: AsyncSession, users: list[User]
) -> dict[uuid.UUID, list[ModelConnection]]:
    ids = [user.id for user in users if user.id is not None]
    if not ids:
        return {}
    rows = await db.execute(select(ModelConnection).where(ModelConnection.user_id.in_(ids)))
    grouped: dict[uuid.UUID, list[ModelConnection]] = {}
    for connection in rows.scalars().all():
        grouped.setdefault(connection.user_id, []).append(connection)
    for items in grouped.values():
        items.sort(key=lambda item: item.name.lower())
    return grouped


async def sync_connections(
    db: AsyncSession, user: User, items: Iterable[dict[str, Any]]
) -> list[ModelConnection]:
    """Replace the account's connection list, keeping untouched keys."""
    cleaned = _clean_connections(items)
    existing = {row.name.lower(): row for row in await list_connections(db, user)}
    keep: set[str] = set()

    for item in cleaned:
        key = item["name"].lower()
        keep.add(key)
        row = existing.get(key)
        if row is None:
            db.add(
                ModelConnection(
                    user_id=user.id,
                    name=item["name"],
                    base_url=item["base_url"],
                    model=item["model"],
                    api_key_enc=encrypt_secret(item["api_key"]) if item["api_key"] else None,
                    supports_tools=True,
                )
            )
            continue
        row.name = item["name"]
        row.base_url = item["base_url"]
        row.model = item["model"]
        if item["clear_key"]:
            row.api_key_enc = None
        elif item["api_key"]:
            row.api_key_enc = encrypt_secret(item["api_key"])

    for key, row in existing.items():
        if key not in keep:
            await db.delete(row)
    await db.flush()
    return await list_connections(db, user)


def connection_to_dict(connection: ModelConnection) -> dict[str, Any]:
    key = decrypt_secret(connection.api_key_enc)
    return {
        "name": connection.name,
        "base_url": connection.base_url,
        "key_masked": mask_secret(key),
        "has_key": bool(key),
        "model": connection.model,
    }


def user_to_dict(
    user: User,
    *,
    stats: dict[str, int] | None = None,
    connections: list[ModelConnection] | None = None,
) -> dict:
    payload: dict = {
        "id": str(user.id),
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
        "connections": [connection_to_dict(item) for item in (connections or [])],
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
    }
    if stats is not None:
        payload["stats"] = stats
    return payload


# ----------------------------------------------------------------- accounts
async def get_user(db: AsyncSession, user_id: uuid.UUID | str) -> User:
    try:
        identifier = uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise UserError("Invalid user id", status_code=400) from exc
    user = await db.get(User, identifier)
    if user is None:
        raise UserError("User not found", status_code=404)
    return user


async def get_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == (username or "").strip().lower()))
    return result.scalars().first()


async def list_users(db: AsyncSession) -> list[User]:
    result = await db.execute(select(User).order_by(User.role, User.username))
    return list(result.scalars().all())


async def create_user(
    db: AsyncSession,
    *,
    username: str,
    password: str,
    display_name: str = "",
    role: str = ROLE_USER,
    connections: Iterable[dict[str, Any]] | None = None,
) -> User:
    clean = normalize_username(username)
    if await get_by_username(db, clean) is not None:
        raise UserError("A user with this username already exists", status_code=409)
    if role not in ROLES:
        raise UserError("Role must be 'admin' or 'user'")
    user = User(
        username=clean,
        display_name=(display_name or "").strip()[:120],
        role=role,
        password_hash=hash_password(validate_password(password)),
        is_active=True,
    )
    db.add(user)
    await db.flush()
    if connections:
        await sync_connections(db, user, connections)
    return user


async def _ensure_other_admin(db: AsyncSession, user: User, *, active_only: bool = False) -> None:
    stmt = select(User.id).where(User.role == ROLE_ADMIN, User.id != user.id)
    if active_only:
        stmt = stmt.where(User.is_active.is_(True))
    result = await db.execute(stmt.limit(1))
    if result.scalars().first() is None:
        raise UserError("At least one active administrator must remain", status_code=409)


async def update_user(
    db: AsyncSession,
    user: User,
    *,
    display_name: str | None = None,
    password: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
    connections: Iterable[dict[str, Any]] | None = None,
) -> User:
    if role is not None:
        if role not in ROLES:
            raise UserError("Role must be 'admin' or 'user'")
        if user.role == ROLE_ADMIN and role != ROLE_ADMIN:
            await _ensure_other_admin(db, user)
        user.role = role
    if is_active is not None:
        if not is_active and user.role == ROLE_ADMIN:
            await _ensure_other_admin(db, user, active_only=True)
        user.is_active = is_active
    if display_name is not None:
        user.display_name = display_name.strip()[:120]
    if password:
        user.password_hash = hash_password(validate_password(password))
        await revoke_tokens(db, user.id)
    if connections is not None:
        await sync_connections(db, user, connections)
    user.updated_at = utcnow()
    await db.flush()
    return user


async def change_password(db: AsyncSession, user: User, current: str, new: str) -> None:
    if not verify_password(current, user.password_hash):
        raise UserError("Current password is incorrect", status_code=403)
    if verify_password(new, user.password_hash):
        raise UserError("The new password must differ from the current one")
    user.password_hash = hash_password(validate_password(new))
    user.updated_at = utcnow()
    await db.flush()


async def delete_user(db: AsyncSession, user: User, *, actor: User) -> dict[str, int]:
    """Delete an account together with its projects, chats and vectors."""
    if user.id == actor.id:
        raise UserError("You cannot delete your own account", status_code=409)
    if user.role == ROLE_ADMIN:
        await _ensure_other_admin(db, user)

    nodes = list((await db.execute(select(Node).where(Node.owner_id == user.id))).scalars().all())
    file_ids = [str(node.id) for node in nodes if node.kind == "file"]
    session_ids = list(
        (await db.execute(select(ChatSession.id).where(ChatSession.owner_id == user.id))).scalars().all()
    )

    if file_ids:
        from app.services.vectorstore import vector_store

        for node_id in file_ids:
            try:
                await vector_store.delete_node(node_id)
            except Exception:  # noqa: BLE001 - vector cleanup is best effort
                logger.debug("Vector cleanup failed for %s", node_id, exc_info=True)

    if session_ids:
        await db.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(session_ids)))
        await db.execute(delete(Operation).where(Operation.session_id.in_(session_ids)))
        await db.execute(delete(ChatSession).where(ChatSession.id.in_(session_ids)))
    await db.execute(delete(AuthToken).where(AuthToken.user_id == user.id))
    await db.execute(delete(ModelConnection).where(ModelConnection.user_id == user.id))
    # Children are removed through the ON DELETE CASCADE self reference.
    for node in nodes:
        if node.parent_id is None:
            await db.delete(node)
    await db.flush()
    await db.execute(delete(Node).where(Node.owner_id == user.id))
    await db.delete(user)
    await db.flush()
    return {"nodes": len(nodes), "sessions": len(session_ids)}


# ------------------------------------------------------------------ sessions
def _token_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)


async def issue_token(db: AsyncSession, user: User) -> tuple[str, datetime]:
    token = new_session_token()
    expires = _token_expiry()
    db.add(AuthToken(token_hash=token_digest(token), user_id=user.id, expires_at=expires))
    await db.flush()
    return token, expires


async def resolve_token(db: AsyncSession, token: str) -> User | None:
    if not token:
        return None
    record = await db.get(AuthToken, token_digest(token))
    if record is None:
        return None
    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        await db.delete(record)
        await db.flush()
        return None
    user = await db.get(User, record.user_id)
    if user is None or not user.is_active:
        return None
    record.last_seen_at = utcnow()
    return user


async def revoke_token(db: AsyncSession, token: str) -> None:
    if not token:
        return
    await db.execute(delete(AuthToken).where(AuthToken.token_hash == token_digest(token)))
    await db.flush()


async def revoke_tokens(db: AsyncSession, user_id: uuid.UUID, *, keep: str | None = None) -> None:
    stmt = delete(AuthToken).where(AuthToken.user_id == user_id)
    if keep:
        stmt = stmt.where(AuthToken.token_hash != token_digest(keep))
    await db.execute(stmt)
    await db.flush()


async def authenticate(db: AsyncSession, username: str, password: str) -> User:
    user = await get_by_username(db, username)
    if user is None or not verify_password(password, user.password_hash):
        raise UserError("Invalid username or password", status_code=401)
    if not user.is_active:
        raise UserError("This account is disabled", status_code=403)
    return user


# --------------------------------------------------------------- bootstrap
async def ensure_admin(db: AsyncSession) -> User | None:
    """Create the bootstrap administrator when the instance has no accounts."""
    if (await db.execute(select(User.id).limit(1))).scalars().first() is not None:
        return None
    admin = await create_user(
        db,
        username=settings.admin_username,
        password=settings.admin_password,
        display_name="Administrator",
        role=ROLE_ADMIN,
    )
    await db.commit()
    logger.info("Bootstrap administrator '%s' created", admin.username)
    return admin
