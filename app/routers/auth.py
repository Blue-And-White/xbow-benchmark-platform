"""Auth: register (gated by registration code), login/logout, me."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import current_user, get_config
from ..models import User
from ..security import generate_api_key, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterIn(BaseModel):
    registration_code: str
    username: str
    password: str


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/register")
async def register(data: RegisterIn, db: AsyncSession = Depends(get_db)) -> dict:
    cfg = await get_config(db)
    if data.registration_code != cfg.registration_code:
        raise HTTPException(403, "invalid registration code")
    if (await db.execute(select(User).where(User.username == data.username))).scalar_one_or_none():
        raise HTTPException(409, "username taken")
    user = User(username=data.username, password_hash=hash_password(data.password))
    db.add(user)
    await db.commit()
    return {"username": user.username, "id": user.id}


@router.post("/login")
async def login(data: LoginIn, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    user = (await db.execute(select(User).where(User.username == data.username))).scalar_one_or_none()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "bad credentials")
    request.session["user_id"] = user.id
    return {"username": user.username, "role": user.role}


@router.post("/logout")
async def logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(current_user)) -> dict:
    return {"id": user.id, "username": user.username, "role": user.role, "created_at": user.created_at.isoformat()}
