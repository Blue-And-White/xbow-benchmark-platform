"""Leaderboard: rank users by solved count + total solve time."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Attempt, AttemptStatus, SolveSheet, User

router = APIRouter(prefix="/leaderboard", tags=["leaderboard"])


@router.get("")
async def leaderboard(db: AsyncSession = Depends(get_db)) -> list[dict]:
    stmt = (
        select(
            User.username,
            func.count(Attempt.id).label("solved"),
            func.coalesce(func.sum(Attempt.solve_duration_ms), 0).label("total_ms"),
        )
        .join(SolveSheet, SolveSheet.user_id == User.id)
        .join(Attempt, Attempt.sheet_id == SolveSheet.id)
        .where(Attempt.status == AttemptStatus.solved.value)
        .group_by(User.id, User.username)
        .order_by(func.count(Attempt.id).desc(), func.sum(Attempt.solve_duration_ms).asc())
    )
    rows = (await db.execute(stmt)).all()
    return [
        {"username": r.username, "solved": int(r.solved), "total_ms": int(r.total_ms)}
        for r in rows
    ]
