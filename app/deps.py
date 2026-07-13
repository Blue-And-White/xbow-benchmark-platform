"""Shared FastAPI dependencies: auth (session user / api-key sheet / admin)."""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .models import PlatformConfig, SolveSheet, User


async def get_config(db: AsyncSession = Depends(get_db)) -> PlatformConfig:
    cfg = (await db.execute(select(PlatformConfig).where(PlatformConfig.id == 1))).scalar_one_or_none()
    if cfg is None:
        cfg = PlatformConfig(
            id=1,
            registration_code=settings.registration_code,
            max_concurrent_per_user=settings.max_concurrent_per_user,
            public_base_url=settings.public_base_url,
            allow_direct_port=settings.allow_direct_port,
        )
        db.add(cfg)
        await db.commit()
        await db.refresh(cfg)
    return cfg


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not logged in")
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if user is None:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user gone")
    if user.disabled:
        request.session.clear()
        raise HTTPException(status_code=403, detail="account disabled")
    return user


async def current_sheet(
    x_api_key: str = Header(default="", alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> SolveSheet:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing X-API-Key")
    sheet = (
        await db.execute(select(SolveSheet).where(SolveSheet.api_key == x_api_key))
    ).scalar_one_or_none()
    if sheet is None:
        raise HTTPException(status_code=401, detail="invalid api key")
    return sheet


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user
