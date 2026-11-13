"""Accounts, sessions (bearer tokens) and per-user provider credentials.

Users are created by an administrator: there is no public sign-up. Passwords are
hashed with PBKDF2-HMAC-SHA256, tokens are random strings of which only the
SHA-256 digest is persisted, and provider API keys (DeepSeek, Qwen) are
encrypted with Fernet.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.catalog import CLOUD_PROVIDERS, PROVIDERS
from app.logging_config import get_logger
from app.models import AuthToken, ChatMessage, ChatSession, Node, Operation, User, utcnow
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
MIN_PASSWORD_LEN = 6


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


# Provider credential columns on the user row. Cloud keys are issued per account;
# the local Qwen server is configured deployment-wide and needs no key.
_USER_KEY_COLUMNS: dict[str, str] = {
    "deepseek": "deepseek_api_key_enc",
    "qwen": "qwen_api_key_enc",
    "qwen-local": "qwen_local_api_key_enc",
}

_ENV_KEY_FALLBACKS: dict[str, tuple[str, ...]] = {
    "deepseek": ("deepseek_api_key",),
    "qwen": ("qwen_api_key", "dashscope_api_key"),
    "qwen-local": ("qwen_local_api_key",),
}


def normalize_provider(provider: str | None) -> str:
    name = (provider or "").strip().lower()
    return name if name in PROVIDERS else "deepseek"


def provider_key(user: User | None, provider: str) -> str:
    """Resolve the API key for one provider.

    Order of precedence: the key assigned to the account, then the
    deployment-wide environment fallback. The local Qwen endpoint may need no
    key at all, in which case an empty string is returned.
    """
    name = normalize_provider(provider)
    if name not in _USER_KEY_COLUMNS:
        if name == "qwen-local":
            return (settings.qwen_local_api_key or "").strip()
        return ""
    if user is not None:
        own = decrypt_secret(getattr(user, _USER_KEY_COLUMNS[name], None))
        if own:
            return own
    for attribute in _ENV_KEY_FALLBACKS.get(name, ()):
        value = (getattr(settings, attribute, "") or "").strip()
        if value:
            return value
    return ""


def provider_model(user: User | None, provider: str) -> str:
    name = normalize_provider(provider)
    if name == "qwen":
        if user is not None and getattr(user, "qwen_model", None):
            return user.qwen_model
        return settings.qwen_model or "qwen-plus"
    if name == "qwen-local":
        if user is not None and getattr(user, "qwen_local_model", None):
            return user.qwen_local_model
        return settings.qwen_local_model or "qwen2.5:7b-instruct"
    return deepseek_model(user)


_MODEL_COLUMNS: dict[str, str] = {
    "deepseek": "deepseek_model",
    "qwen": "qwen_model",
    "qwen-local": "qwen_local_model",
}


def provider_model_override(user: User | None, provider: str) -> str:
    """The model stored on the account itself ("" means "use the default")."""
    column = _MODEL_COLUMNS.get(normalize_provider(provider))
    if user is None or not column:
        return ""
    return (getattr(user, column, None) or "").strip()


def default_provider_endpoint(provider: str) -> str:
    name = normalize_provider(provider)
    if name == "qwen":
        return (settings.qwen_base_url or "").strip()
    if name == "qwen-local":
        return (settings.qwen_local_base_url or "").strip()
    return (settings.deepseek_base_url or "").strip()


def provider_endpoint_override(user: User | None, provider: str) -> str:
    """The endpoint stored on the account itself ("" means "use the default")."""
    if user is None:
        return ""
    mapping = getattr(user, "provider_base_urls", None) or {}
    value = mapping.get(normalize_provider(provider))
    return value.strip() if isinstance(value, str) else ""


def provider_endpoint(user: User | None, provider: str) -> str:
    """Effective endpoint: per-account override, then the deployment default."""
    return provider_endpoint_override(user, provider) or default_provider_endpoint(provider)


def provider_is_available(user: User | None, provider: str) -> bool:
    meta = PROVIDERS.get(normalize_provider(provider))
    if meta is not None and meta.local:
        return bool(provider_endpoint(user, provider))
    return bool(provider_key(user, provider))


def provider_source(user: User | None, provider: str) -> str:
    name = normalize_provider(provider)
    if has_personal_key(user, name):
        return "user"
    meta = PROVIDERS.get(name)
    if meta is not None and meta.local and provider_endpoint(user, name):
        return "local"
    if provider_key(user, name):
        return "environment"
    return "none"


def sanitize_provider_endpoints(values: dict[str, str] | None) -> dict[str, str]:
    """Keep known providers only, trim values and drop empty entries."""
    if not values:
        return {}
    cleaned: dict[str, str] = {}
    for provider, raw in values.items():
        name = (provider or "").strip().lower()
        if name not in PROVIDERS or not isinstance(raw, str):
            continue
        value = raw.strip()[:400]
        if value:
            cleaned[name] = value
    return cleaned


def personal_key(user: User | None, provider: str) -> str:
    """The key stored on the account itself (ignores environment fallbacks)."""
    column = _USER_KEY_COLUMNS.get(normalize_provider(provider))
    if user is None or not column:
        return ""
    return decrypt_secret(getattr(user, column, None))


def has_personal_key(user: User | None, provider: str) -> bool:
    return bool(personal_key(user, provider))


def has_any_personal_key(user: User | None) -> bool:
    return any(has_personal_key(user, provider) for provider in CLOUD_PROVIDERS)


def has_any_provider(user: User | None) -> bool:
    """True when at least one provider is usable (per-user or deployment-wide)."""
    return any(provider_is_available(user, provider) for provider in PROVIDERS)


def deepseek_key(user: User | None) -> str:
    """The user's own DeepSeek key, falling back to the deployment-wide key."""
    return provider_key(user, "deepseek")


def deepseek_model(user: User | None) -> str:
    if user is not None and getattr(user, "deepseek_model", None):
        return user.deepseek_model
    return settings.llm_model or "deepseek-chat"


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
    deepseek_api_key: str | None = None,
    deepseek_model: str | None = None,
    qwen_api_key: str | None = None,
    qwen_model: str | None = None,
    qwen_local_api_key: str | None = None,
    qwen_local_model: str | None = None,
    provider_base_urls: dict[str, str] | None = None,
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
        deepseek_api_key_enc=encrypt_secret(deepseek_api_key.strip()) if deepseek_api_key else None,
        deepseek_model=(deepseek_model or "").strip()[:64] or None,
        qwen_api_key_enc=encrypt_secret(qwen_api_key.strip()) if qwen_api_key else None,
        qwen_model=(qwen_model or "").strip()[:64] or None,
        qwen_local_api_key_enc=encrypt_secret(qwen_local_api_key.strip())
        if qwen_local_api_key
        else None,
        qwen_local_model=(qwen_local_model or "").strip()[:64] or None,
        provider_base_urls=sanitize_provider_endpoints(provider_base_urls) or None,
    )

    db.add(user)
    await db.flush()
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
    deepseek_api_key: str | None = None,
    deepseek_model: str | None = None,
    clear_deepseek_key: bool = False,
    qwen_api_key: str | None = None,
    qwen_model: str | None = None,
    clear_qwen_key: bool = False,
    qwen_local_api_key: str | None = None,
    qwen_local_model: str | None = None,
    clear_qwen_local_key: bool = False,
    provider_base_urls: dict[str, str] | None = None,
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
    if clear_deepseek_key:
        user.deepseek_api_key_enc = None
    elif deepseek_api_key is not None:
        value = deepseek_api_key.strip()
        user.deepseek_api_key_enc = encrypt_secret(value) if value else None
    if deepseek_model is not None:
        user.deepseek_model = deepseek_model.strip()[:64] or None
    if clear_qwen_key:
        user.qwen_api_key_enc = None
    elif qwen_api_key is not None:
        value = qwen_api_key.strip()
        user.qwen_api_key_enc = encrypt_secret(value) if value else None
    if qwen_model is not None:
        user.qwen_model = qwen_model.strip()[:64] or None
    if clear_qwen_local_key:
        user.qwen_local_api_key_enc = None
    elif qwen_local_api_key is not None:
        value = qwen_local_api_key.strip()
        user.qwen_local_api_key_enc = encrypt_secret(value) if value else None
    if qwen_local_model is not None:
        user.qwen_local_model = qwen_local_model.strip()[:64] or None
    if provider_base_urls is not None:
        user.provider_base_urls = sanitize_provider_endpoints(provider_base_urls) or None

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


# ------------------------------------------------------------------ helpers
def user_to_dict(user: User, *, stats: dict[str, int] | None = None) -> dict:
    keys = {provider: personal_key(user, provider) for provider in CLOUD_PROVIDERS}
    providers = {
        name: {
            "configured": provider_is_available(user, name),
            "key_configured": bool(personal_key(user, name)),
            "key_masked": mask_secret(personal_key(user, name)),
            "source": provider_source(user, name),
            "model": provider_model(user, name),
            "model_override": provider_model_override(user, name),
            "default_model": provider_model(None, name),
            "base_url": provider_endpoint(user, name),
            "base_url_override": provider_endpoint_override(user, name),
            "default_base_url": default_provider_endpoint(name),
            "requires_api_key": meta.requires_api_key,
            "local": meta.local,
        }
        for name, meta in PROVIDERS.items()
    }
    payload: dict = {
        "id": str(user.id),
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
        "deepseek_configured": bool(keys.get("deepseek")),
        "deepseek_key_masked": mask_secret(keys.get("deepseek")),
        "deepseek_model": user.deepseek_model or "",
        "qwen_configured": bool(keys.get("qwen")),
        "qwen_key_masked": mask_secret(keys.get("qwen")),
        "qwen_model": user.qwen_model or "",
        "providers": providers,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
    }
    if stats is not None:
        payload["stats"] = stats
    return payload
