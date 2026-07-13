"""Admin: platform config + user management + image status."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import docker_ops
from ..db import get_db
from ..deps import current_user, get_config, require_admin
from ..models import PlatformConfig, SolveSheet, User, Challenge

router = APIRouter(prefix="/admin", tags=["admin"])


class SettingsIn(BaseModel):
    registration_code: str | None = None
    max_concurrent_per_user: int | None = None
    public_base_url: str | None = None
    allow_direct_port: bool | None = None


@router.get("/settings")
async def get_settings(admin: User = Depends(require_admin), cfg: PlatformConfig = Depends(get_config)) -> dict:
    return {
        "registration_code": cfg.registration_code,
        "max_concurrent_per_user": cfg.max_concurrent_per_user,
        "public_base_url": cfg.public_base_url,
        "allow_direct_port": cfg.allow_direct_port,
    }


@router.put("/settings")
async def put_settings(
    data: SettingsIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    cfg: PlatformConfig = Depends(get_config),
) -> dict:
    if data.registration_code is not None:
        cfg.registration_code = data.registration_code
    if data.max_concurrent_per_user is not None:
        cfg.max_concurrent_per_user = data.max_concurrent_per_user
    if data.public_base_url is not None:
        cfg.public_base_url = data.public_base_url
    if data.allow_direct_port is not None:
        cfg.allow_direct_port = data.allow_direct_port
    await db.commit()
    return {"ok": True}


@router.get("/users")
async def list_users(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> list[dict]:
    users = (await db.execute(select(User).order_by(User.id))).scalars().all()
    out = []
    for u in users:
        sheets = (await db.execute(select(SolveSheet).where(SolveSheet.user_id == u.id))).scalars().all()
        out.append({"id": u.id, "username": u.username, "role": u.role, "sheets": len(sheets), "created_at": u.created_at.isoformat()})
    return out


@router.get("/images")
async def image_status(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
    """Check which of the 104 benchmarks have images built locally."""
    challs = (await db.execute(select(Challenge).order_by(Challenge.benchmark))).scalars().all()
    built, missing = [], []
    for c in challs:
        if c.service and await docker_ops.image_exists(c.benchmark, c.service):
            built.append(c.benchmark)
        else:
            missing.append({"benchmark": c.benchmark, "level": c.level, "supported": c.supported, "service": c.service})
    return {"total": len(challs), "built": len(built), "missing": len(missing),
            "built_list": built, "missing_list": missing}
