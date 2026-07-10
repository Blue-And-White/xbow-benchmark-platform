"""Shared challenge lifecycle logic (used by both the API router and UI pages)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import docker_ops
from .config import settings
from .models import Attempt, AttemptStatus, Challenge, PlatformConfig, SolveSheet


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _count_in_progress(user_id: int, db: AsyncSession) -> int:
    stmt = (
        select(func.count(Attempt.id))
        .join(SolveSheet, SolveSheet.id == Attempt.sheet_id)
        .where(SolveSheet.user_id == user_id, Attempt.status == AttemptStatus.in_progress.value)
    )
    return int((await db.execute(stmt)).scalar() or 0)


def urls_for(cfg: PlatformConfig, att: Attempt) -> dict:
    base = (cfg.public_base_url or "").rstrip("/")
    out = {"url": f"{base}/c/{att.id}/"}
    if cfg.allow_direct_port and att.host_port:
        out["direct_url"] = f"http://127.0.0.1:{att.host_port}/"
    return out


async def start(db: AsyncSession, sheet: SolveSheet, c: Challenge, cfg: PlatformConfig) -> dict:
    if not c.supported:
        raise ServiceError(409, "challenge not supported by platform")
    if not c.service or not await docker_ops.image_exists(c.benchmark, c.service):
        raise ServiceError(409, "challenge image not built — run prebuild")

    att = (
        await db.execute(select(Attempt).where(Attempt.sheet_id == sheet.id, Attempt.challenge_id == c.id))
    ).scalar_one_or_none()
    if att and att.status == AttemptStatus.solved.value:
        raise ServiceError(409, "already solved")
    if att and att.status == AttemptStatus.in_progress.value:
        await db.refresh(att)
        return {"attempt_id": att.id, "benchmark": c.benchmark, "status": "in_progress", **urls_for(cfg, att)}

    if await _count_in_progress(sheet.user_id, db) >= cfg.max_concurrent_per_user:
        raise ServiceError(409, f"concurrency limit reached ({cfg.max_concurrent_per_user} running)")

    # fixed mode (e.g. flag baked into a DB init .sql): use the baked flag as-is.
    # otherwise generate a fresh random flag (file/env/embedded are injected at start).
    if c.flag_type == "fixed":
        dynamic_flag = c.original_flag or ""
    else:
        dynamic_flag = f"FLAG{{{uuid.uuid4().hex}}}"
    if att is None:
        att = Attempt(sheet_id=sheet.id, challenge_id=c.id)
        db.add(att)
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
        att.status = AttemptStatus.abandoned.value
        att.dynamic_flag = None
        att.started_at = None
        await db.commit()
        raise ServiceError(500, f"failed to start challenge: {e}")

    att.compose_project = inst.project
    att.host_port = inst.host_port
    await db.commit()
    await db.refresh(att)
    return {"attempt_id": att.id, "benchmark": c.benchmark, "status": "in_progress", **urls_for(cfg, att)}


async def submit(db: AsyncSession, sheet: SolveSheet, c: Challenge, flag: str) -> dict:
    att = (
        await db.execute(select(Attempt).where(Attempt.sheet_id == sheet.id, Attempt.challenge_id == c.id))
    ).scalar_one_or_none()
    if not att or att.status != AttemptStatus.in_progress.value:
        raise ServiceError(409, "no active attempt; start the challenge first")

    submitted = (flag or "").strip()
    if not att.dynamic_flag or submitted != att.dynamic_flag:
        return {"correct": False, "benchmark": c.benchmark}

    now = _now()
    att.status = AttemptStatus.solved.value
    att.solved_at = now
    if att.started_at:
        started = att.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        att.solve_duration_ms = int((now - started).total_seconds() * 1000)
    project, work_dir = att.compose_project, settings.runs_dir / f"{c.benchmark}_{att.id}"
    if project:
        try:
            await docker_ops.stop_challenge(project, work_dir)
        except Exception:
            pass
    att.compose_project = None
    att.host_port = None
    att.dynamic_flag = None
    await db.commit()
    return {"correct": True, "benchmark": c.benchmark, "solve_duration_ms": att.solve_duration_ms}


async def stop(db: AsyncSession, sheet: SolveSheet, c: Challenge) -> dict:
    att = (
        await db.execute(select(Attempt).where(Attempt.sheet_id == sheet.id, Attempt.challenge_id == c.id))
    ).scalar_one_or_none()
    if not att or att.status != AttemptStatus.in_progress.value:
        return {"stopped": False, "benchmark": c.benchmark, "note": "nothing running"}
    project, work_dir = att.compose_project, settings.runs_dir / f"{c.benchmark}_{att.id}"
    if project:
        try:
            await docker_ops.stop_challenge(project, work_dir)
        except Exception:
            pass
    att.status = AttemptStatus.abandoned.value
    att.compose_project = None
    att.host_port = None
    att.dynamic_flag = None
    att.started_at = None
    att.solved_at = None
    att.solve_duration_ms = None
    await db.commit()
    return {"stopped": True, "benchmark": c.benchmark}
