"""Solve sheets: each sheet = an independent 104-board with its own api-key."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import current_user
from ..models import SolveSheet, User
from ..security import generate_api_key

router = APIRouter(prefix="/sheets", tags=["sheets"])


class SheetIn(BaseModel):
    name: str


@router.get("")
async def list_sheets(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(SolveSheet).where(SolveSheet.user_id == user.id).order_by(SolveSheet.id))).scalars().all()
    return [
        {"id": s.id, "name": s.name, "api_key": s.api_key, "created_at": s.created_at.isoformat()}
        for s in rows
    ]


@router.post("")
async def create_sheet(data: SheetIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    name = data.name.strip() or "sheet"
    sheet = SolveSheet(user_id=user.id, name=name[:128], api_key=generate_api_key())
    db.add(sheet)
    await db.commit()
    await db.refresh(sheet)
    return {"id": sheet.id, "name": sheet.name, "api_key": sheet.api_key, "created_at": sheet.created_at.isoformat()}
