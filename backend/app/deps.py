"""Shared FastAPI dependencies: bearer-token authentication and RBAC."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User
from app.services import users as user_service


def bearer_token(authorization: str | None) -> str:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""
    if not authorization:
        return ""
    scheme, _, value = authorization.partition(" ")
    if scheme.strip().lower() != "bearer":
        return ""
    return value.strip()


def unauthorized(message: str = "Authentication required") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=message,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def current_user(
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
    token: str | None = None,
) -> User:
    """Resolve the account behind the bearer token.

    A ``?token=`` query parameter is accepted as a fallback because browsers do
    not attach headers to ``<img>``/``<iframe>`` downloads (file previews).
    """
    raw = bearer_token(authorization) or (token or "")
    user = await user_service.resolve_token(db, raw)
    if user is None:
        raise unauthorized("Invalid or expired session")
    return user


async def current_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator rights required"
        )
    return user


CurrentUser = Depends(current_user)
CurrentAdmin = Depends(current_admin)
