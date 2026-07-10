"""Challenges API: list (per-sheet status), start, submit, stop (api-key auth)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import current_sheet, get_config
from ..models import Attempt, Challenge, SolveSheet
from ..service import ServiceError, start as svc_start, stop as svc_stop, submit as svc_submit

router = APIRouter(prefix="/challenges", tags=["challenges"])


class SubmitIn(BaseModel):
    flag: str


def _view(c: Challenge, att: Attempt | None) -> dict:
    status = "not_started"
    started_at = solved_at = dur = None
    if att:
        if att.status == "solved":
            status = "solved"
            solved_at = att.solved_at.isoformat() if att.solved_at else None
            dur = att.solve_duration_ms
        elif att.status == "in_progress":
            status = "in_progress"
            started_at = att.started_at.isoformat() if att.started_at else None
    return {
        "benchmark": c.benchmark, "title": c.title, "level": c.level,
        "tags": c.tags.split(",") if c.tags else [], "win_condition": c.win_condition,
        "supported": c.supported, "status": status, "started_at": started_at,
        "solved_at": solved_at, "solve_duration_ms": dur,
    }


async def _get_challenge(db: AsyncSession, benchmark: str) -> Challenge:
    c = (await db.execute(select(Challenge).where(Challenge.benchmark == benchmark))).scalar_one_or_none()
    if c is None:
        raise HTTPException(404, "no such challenge")
    return c


@router.get("")
async def list_challenges(sheet: SolveSheet = Depends(current_sheet), db: AsyncSession = Depends(get_db)) -> list[dict]:
    challs = (await db.execute(select(Challenge).order_by(Challenge.benchmark))).scalars().all()
    atts = {a.challenge_id: a for a in
            (await db.execute(select(Attempt).where(Attempt.sheet_id == sheet.id))).scalars().all()}
    return [_view(c, atts.get(c.id)) for c in challs]


def _wrap(coro):
    """Convert ServiceError -> HTTPException (kept for potential reuse)."""
    async def _run():
        try:
            return await coro
        except ServiceError as e:
            raise HTTPException(e.status_code, e.detail)
    return _run()


@router.post("/{benchmark}/start")
async def start(benchmark: str, sheet: SolveSheet = Depends(current_sheet),
                db: AsyncSession = Depends(get_db), cfg=Depends(get_config)) -> dict:
    c = await _get_challenge(db, benchmark)
    try:
        return await svc_start(db, sheet, c, cfg)
    except ServiceError as e:
        raise HTTPException(e.status_code, e.detail)


@router.post("/{benchmark}/submit")
async def submit(benchmark: str, data: SubmitIn, sheet: SolveSheet = Depends(current_sheet),
                 db: AsyncSession = Depends(get_db)) -> dict:
    c = await _get_challenge(db, benchmark)
    try:
        return await svc_submit(db, sheet, c, data.flag)
    except ServiceError as e:
        raise HTTPException(e.status_code, e.detail)


@router.post("/{benchmark}/stop")
async def stop(benchmark: str, sheet: SolveSheet = Depends(current_sheet),
               db: AsyncSession = Depends(get_db)) -> dict:
    c = await _get_challenge(db, benchmark)
    return await svc_stop(db, sheet, c)
