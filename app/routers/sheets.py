"""Solve sheets: each sheet = an independent 104-board with its own api-key."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import current_user
from ..models import Attempt, Challenge, SolveSheet, User
from ..security import generate_api_key
from ..service import delete_sheet as svc_delete_sheet

router = APIRouter(prefix="/sheets", tags=["sheets"])


class SheetIn(BaseModel):
    name: str


async def _owned(db: AsyncSession, user: User, sheet_id: int) -> SolveSheet:
    s = (await db.execute(select(SolveSheet).where(SolveSheet.id == sheet_id, SolveSheet.user_id == user.id))).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "sheet not found")
    return s


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


@router.delete("/{sheet_id}")
async def delete_sheet(sheet_id: int, force: bool = False, user: User = Depends(current_user),
                       db: AsyncSession = Depends(get_db)) -> dict:
    sheet = await _owned(db, user, sheet_id)
    try:
        return await svc_delete_sheet(db, sheet, force=force)
    except ServiceError as e:
        raise HTTPException(e.status_code, e.detail)


@router.get("/{sheet_id}/export")
async def export_sheet(sheet_id: int, user: User = Depends(current_user),
                       db: AsyncSession = Depends(get_db)) -> dict:
    sheet = await _owned(db, user, sheet_id)
    challs = (await db.execute(select(Challenge).order_by(Challenge.benchmark))).scalars().all()
    atts = {a.challenge_id: a for a in
            (await db.execute(select(Attempt).where(Attempt.sheet_id == sheet.id))).scalars().all()}
    rows, solved, running = [], 0, 0
    for c in challs:
        a = atts.get(c.id)
        st = "solved" if a and a.status == "solved" else ("in_progress" if a and a.status == "in_progress" else "not_started")
        if st == "solved": solved += 1
        elif st == "in_progress": running += 1
        rows.append({
            "benchmark": c.benchmark, "title": c.title, "level": c.level,
            "tags": c.tags.split(",") if c.tags else [], "status": st,
            "started_at": a.started_at.isoformat() if a and a.started_at else None,
            "solved_at": a.solved_at.isoformat() if a and a.solved_at else None,
            "solve_duration_ms": a.solve_duration_ms if a else None,
        })
    return {
        "sheet": {"id": sheet.id, "name": sheet.name, "api_key": sheet.api_key,
                  "created_at": sheet.created_at.isoformat()},
        "summary": {"total": len(challs), "solved": solved, "running": running, "unsolved": len(challs) - solved},
        "challenges": rows,
    }