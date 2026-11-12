"""Login, logout, current account and password endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import bearer_token, current_user
from app.logging_config import get_logger
from app.models import User
from app.schemas import LoginIn, LoginOut, PasswordChangeIn, UserOut
from app.services import users as user_service

logger = get_logger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])
CurrentUser = Depends(current_user)


def _public(user: User) -> UserOut:
    return UserOut.model_validate(user_service.user_to_dict(user))


@router.post("/login", response_model=LoginOut)
async def login(payload: LoginIn, db: AsyncSession = Depends(get_db)) -> LoginOut:
    user = await user_service.authenticate(db, payload.username, payload.password)
    token, expires = await user_service.issue_token(db, user)
    await db.commit()
    logger.info("User '%s' signed in", user.username)
    return LoginOut(token=token, expires_at=expires, user=_public(user))


@router.post("/logout")
async def logout(
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
    authorization: str | None = Header(default=None),
) -> dict:
    await user_service.revoke_token(db, bearer_token(authorization))
    await db.commit()
    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def me(user: User = CurrentUser) -> UserOut:
    return _public(user)


@router.post("/password")
async def change_password(
    payload: PasswordChangeIn,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentUser,
    authorization: str | None = Header(default=None),
) -> dict:
    await user_service.change_password(db, user, payload.current_password, payload.new_password)
    # Keep the session that performed the change, drop every other one.
    await user_service.revoke_tokens(db, user.id, keep=bearer_token(authorization))
    await db.commit()
    return {"ok": True}
