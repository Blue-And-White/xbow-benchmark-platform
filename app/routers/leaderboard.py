"""Leaderboard: rank by (user, board) — solved count desc, then total time asc."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Attempt, AttemptStatus, SolveSheet, User

router = APIRouter(prefix="/leaderboard", tags=["leaderboard"])


@router.get("")
async def leaderboard(db: AsyncSession = Depends(get_db)) -> list[dict]:
    solved = func.sum(case((Attempt.status == AttemptStatus.solved.value, 1), else_=0))
    total_ms = func.coalesce(
        func.sum(case((Attempt.status == AttemptStatus.solved.value, Attempt.solve_duration_ms), else_=0)),
        0,
    )
    stmt = (
        select(User.username, SolveSheet.id, SolveSheet.name, solved, total_ms)
        .select_from(SolveSheet)
        .join(User, SolveSheet.user_id == User.id)
        .outerjoin(Attempt, Attempt.sheet_id == SolveSheet.id)  # keep 0-solved boards too
        .group_by(SolveSheet.id, User.username, SolveSheet.name)
        .order_by(solved.desc(), total_ms.asc(), SolveSheet.id)
    )
    rows = (await db.execute(stmt)).all()
    out = []
    for i, r in enumerate(rows):
        out.append({
            "rank": i + 1,
            "username": r[0],
            "sheet_id": r[1],
            "sheet_name": r[2],
            "solved": int(r[3] or 0),
            "total_ms": int(r[4] or 0),
        })
    return out
