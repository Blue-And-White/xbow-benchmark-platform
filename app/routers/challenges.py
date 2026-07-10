"""Challenges: list (per-sheet status), start, submit, stop."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import docker_ops
from ..db import get_db
from ..deps import current_sheet, get_config
from ..models import Attempt, AttemptStatus, Challenge, SolveSheet

router = APIRouter(prefix="/challenges", tags=["challenges"])


class SubmitIn(BaseModel):
    flag: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _count_in_progress(user_id: int, db: AsyncSession) -> int:
    # across ALL the user's sheets (per-user concurrent cap)
    stmt = (
        select(func.count(Attempt.id))
        .join(SolveSheet, SolveSheet.id == Attempt.sheet_id)
        .where(SolveSheet.user_id == user_id, Attempt.status == AttemptStatus.in_progress.value)
    )
    return int((await db.execute(stmt)).scalar() or 0)


def _challenge_view(c: Challenge, att: Attempt | None) -> dict:
    status_val = AttemptStatus.not_started.value
    started_at = solved_at = solve_duration_ms = None
    if att:
        if att.status == AttemptStatus.solved.value:
            status_val = "solved"
            solved_at = att.solved_at.isoformat() if att.solved_at else None
            solve_duration_ms = att.solve_duration_ms
        elif att.status == AttemptStatus.in_progress.value:
            status_val = "in_progress"
            started_at = att.started_at.isoformat() if att.started_at else None
        else:  # abandoned
            status_val = "not_started"
    return {
        "benchmark": c.benchmark,
        "title": c.title,
        "level": c.level,
        "tags": c.tags.split(",") if c.tags else [],
        "win_condition": c.win_condition,
        "supported": c.supported,
        "status": status_val,
        "started_at": started_at,
        "solved_at": solved_at,
        "solve_duration_ms": solve_duration_ms,
    }


@router.get("")
async def list_challenges(sheet: SolveSheet = Depends(current_sheet), db: AsyncSession = Depends(get_db)) -> list[dict]:
    challs = (await db.execute(select(Challenge).order_by(Challenge.benchmark))).scalars().all()
    atts = {
        a.challenge_id: a
        for a in (
            await db.execute(select(Attempt).where(Attempt.sheet_id == sheet.id))
        ).scalars().all()
    }
    return [_challenge_view(c, atts.get(c.id)) for c in challs]


@router.post("/{benchmark}/start")
async def start_challenge(
    benchmark: str,
    sheet: SolveSheet = Depends(current_sheet),
    db: AsyncSession = Depends(get_db),
    cfg=Depends(get_config),
) -> dict:
    c = (await db.execute(select(Challenge).where(Challenge.benchmark == benchmark))).scalar_one_or_none()
    if c is None:
        raise HTTPException(404, "no such challenge")
    if not c.supported:
        raise HTTPException(409, "challenge not supported by platform")
    if not c.service or not await docker_ops.image_exists(c.benchmark, c.service):
        raise HTTPException(409, "challenge image not built — run prebuild")

    # existing attempt?
    att = (
        await db.execute(select(Attempt).where(Attempt.sheet_id == sheet.id, Attempt.challenge_id == c.id))
    ).scalar_one_or_none()
    if att and att.status == AttemptStatus.solved.value:
        raise HTTPException(409, "already solved")
    if att and att.status == AttemptStatus.in_progress.value:
        # idempotent: return the running attempt
        url = _urls(cfg, att)
        return {"attempt_id": att.id, "benchmark": benchmark, "status": "in_progress", **url}

    # concurrency cap (per user)
    if await _count_in_progress(sheet.user_id, db) >= cfg.max_concurrent_per_user:
        raise HTTPException(409, f"concurrency limit reached ({cfg.max_concurrent_per_user} running)")

    dynamic_flag = f"FLAG{{{uuid.uuid4().hex}}}"
    if att is None:
        att = Attempt(sheet_id=sheet.id, challenge_id=c.id)
        db.add(att)
    # mark in_progress first so the cap counts even if start is slow
    att.status = AttemptStatus.in_progress.value
    att.dynamic_flag = dynamic_flag
    att.started_at = _now()
    att.solved_at = None
    att.solve_duration_ms = None
    await db.commit()
    await db.refresh(att)

    try:
        inst = await docker_ops.start_challenge(c.benchmark, att.id, dynamic_flag)
    except Exception as e:
        # roll back the attempt
        att.status = AttemptStatus.abandoned.value
        att.dynamic_flag = None
        att.started_at = None
        await db.commit()
        raise HTTPException(500, f"failed to start challenge: {e}")

    att.compose_project = inst.project
    att.host_port = inst.host_port
    await db.commit()
    await db.refresh(att)
    return {"attempt_id": att.id, "benchmark": benchmark, "status": "in_progress", **_urls(cfg, att)}


@router.post("/{benchmark}/submit")
async def submit_flag(
    benchmark: str,
    data: SubmitIn,
    sheet: SolveSheet = Depends(current_sheet),
    db: AsyncSession = Depends(get_db),
) -> dict:
    c = (await db.execute(select(Challenge).where(Challenge.benchmark == benchmark))).scalar_one_or_none()
    if c is None:
        raise HTTPException(404, "no such challenge")
    att = (
        await db.execute(select(Attempt).where(Attempt.sheet_id == sheet.id, Attempt.challenge_id == c.id))
    ).scalar_one_or_none()
    if not att or att.status != AttemptStatus.in_progress.value:
        raise HTTPException(409, "no active attempt; start the challenge first")

    submitted = (data.flag or "").strip()
    if not att.dynamic_flag or submitted != att.dynamic_flag:
        return {"correct": False, "benchmark": benchmark}

    # correct -> solve
    now = _now()
    att.status = AttemptStatus.solved.value
    att.solved_at = now
    if att.started_at:
        started = att.started_at
        if started.tzinfo is None:           # SQLite returns naive datetimes
            started = started.replace(tzinfo=timezone.utc)
        att.solve_duration_ms = int((now - started).total_seconds() * 1000)
    project, work_dir = att.compose_project, None
    # stop + remove the container
    from ..config import settings
    work_dir = settings.runs_dir / f"{benchmark}_{att.id}"
    if project:
        try:
            await docker_ops.stop_challenge(project, work_dir)
        except Exception:
            pass
    att.compose_project = None
    att.host_port = None
    att.dynamic_flag = None
    await db.commit()
    return {"correct": True, "benchmark": benchmark, "solve_duration_ms": att.solve_duration_ms}


@router.post("/{benchmark}/stop")
async def stop_challenge(
    benchmark: str,
    sheet: SolveSheet = Depends(current_sheet),
    db: AsyncSession = Depends(get_db),
) -> dict:
    c = (await db.execute(select(Challenge).where(Challenge.benchmark == benchmark))).scalar_one_or_none()
    if c is None:
        raise HTTPException(404, "no such challenge")
    att = (
        await db.execute(select(Attempt).where(Attempt.sheet_id == sheet.id, Attempt.challenge_id == c.id))
    ).scalar_one_or_none()
    if not att or att.status != AttemptStatus.in_progress.value:
        return {"stopped": False, "benchmark": benchmark, "note": "nothing running"}

    project = att.compose_project
    from ..config import settings
    work_dir = settings.runs_dir / f"{benchmark}_{att.id}"
    if project:
        try:
            await docker_ops.stop_challenge(project, work_dir)
        except Exception:
            pass
    # clear this attempt's dynamic flag + start time, mark abandoned (board -> not_started)
    att.status = AttemptStatus.abandoned.value
    att.compose_project = None
    att.host_port = None
    att.dynamic_flag = None
    att.started_at = None
    att.solved_at = None
    att.solve_duration_ms = None
    await db.commit()
    return {"stopped": True, "benchmark": benchmark}


def _urls(cfg, att: Attempt) -> dict:
    out: dict = {}
    base = (cfg.public_base_url or "").rstrip("/")
    out["url"] = f"{base}/c/{att.id}/"
    if cfg.allow_direct_port and att.host_port:
        out["direct_url"] = f"http://127.0.0.1:{att.host_port}/"
    return out
