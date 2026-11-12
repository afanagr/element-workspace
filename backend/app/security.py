"""Secret handling: password hashing, bearer tokens, Fernet keys + masking."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)

PBKDF2_ITERATIONS = 260_000
_PASSWORD_SCHEME = "pbkdf2_sha256"


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.app_secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


# ----------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256 (stdlib only, no extra deps)."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), PBKDF2_ITERATIONS)
    return f"{_PASSWORD_SCHEME}${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, iterations, salt, expected = stored.split("$", 3)
        if scheme != _PASSWORD_SCHEME:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("ascii"), int(iterations)
        )
    except (ValueError, TypeError):
        logger.warning("Stored password hash has an unexpected format")
        return False
    return hmac.compare_digest(digest.hex(), expected)


# --------------------------------------------------------------------- tokens
def new_session_token() -> str:
    return secrets.token_urlsafe(36)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# -------------------------------------------------------------------- secrets
def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str | None) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.warning("Stored secret could not be decrypted (APP_SECRET_KEY changed?)")
        return ""


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
