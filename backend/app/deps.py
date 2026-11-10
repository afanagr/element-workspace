"""Shared FastAPI dependencies (single-user auth stub, DB session)."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status

from app.config import settings

SINGLE_USER = "local-user"


async def current_user(authorization: str | None = Header(default=None)) -> str:
    """Single-user auth stub.

    If ``APP_USER_TOKEN`` is set the client must send ``Authorization: Bearer <token>``.
    Otherwise the workspace is open (intended for local / single-user deployments).
    """
    if not settings.app_user_token:
        return SINGLE_USER
    expected = f"Bearer {settings.app_user_token}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing token")
    return SINGLE_USER


CurrentUser = Depends(current_user)
