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


async def _count_in_progress(sheet_id: int, db: AsyncSession) -> int:
    """Count in_progress attempts for THIS SHEET (not per-user)."""
    stmt = (
        select(func.count(Attempt.id))
        .where(Attempt.sheet_id == sheet_id, Attempt.status == AttemptStatus.in_progress.value)
    )
    return int((await db.execute(stmt)).scalar() or 0)


async def _running_in_sheet(sheet_id: int, db: AsyncSession) -> list[dict]:
    """Return list of in_progress challenges in this sheet (for error message)."""
    stmt = (
        select(Challenge.benchmark, Challenge.title)
        .join(Attempt, Attempt.challenge_id == Challenge.id)
        .where(Attempt.sheet_id == sheet_id, Attempt.status == AttemptStatus.in_progress.value)
    )
    rows = (await db.execute(stmt)).all()
    return [{"benchmark": r[0], "title": r[1]} for r in rows]


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

    if await _count_in_progress(sheet.id, db) >= cfg.max_concurrent_per_user:
        running = await _running_in_sheet(sheet.id, db)
        names = ", ".join(f"{r['benchmark']}" for r in running)
        raise ServiceError(409, f"此看板已启动 {len(running)}/{cfg.max_concurrent_per_user} 个题目，正在作答中: {names}")

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


async def delete_sheet(db: AsyncSession, sheet: SolveSheet, force: bool = False) -> dict:
    """Delete a sheet. If force=False and there are running challenges, refuse.
    If force=True, stop running ones then delete."""
    atts = (await db.execute(
        select(Attempt).where(Attempt.sheet_id == sheet.id)
    )).scalars().all()
    running = [a for a in atts if a.status == AttemptStatus.in_progress.value]
    if running and not force:
        chall_ids = {a.challenge_id for a in running}
        challs = (await db.execute(select(Challenge).where(Challenge.id.in_(chall_ids)))).scalars().all()
        names = ", ".join(c.benchmark for c in challs)
        raise ServiceError(409, f"此看板有 {len(running)} 个题目正在作答中({names})，"
                         f"请先停止它们，或勾选「关闭题目并删除」")

    # need benchmark names to build the work_dir path
    chall_ids = {a.challenge_id for a in atts}
    chall_map: dict[int, Challenge] = {}
    if chall_ids:
        for c in (await db.execute(select(Challenge).where(Challenge.id.in_(chall_ids)))).scalars().all():
            chall_map[c.id] = c
    for a in running:
        c = chall_map.get(a.challenge_id)
        wd = settings.runs_dir / f"{c.benchmark}_{a.id}" if c else None
        if a.compose_project:
            try:
                await docker_ops.stop_challenge(a.compose_project, wd or settings.runs_dir / f"_{a.id}")
            except Exception:
                pass
    # delete attempts explicitly (SQLite FK cascade may be off)
    for a in atts:
        await db.delete(a)
    await db.delete(sheet)
    await db.commit()
    return {"deleted": True, "id": sheet.id}


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
    # KEEP started_at — record of attempt preserved (prevent "reset = cheat")
    att.solved_at = None
    att.solve_duration_ms = None
    await db.commit()
    return {"stopped": True, "benchmark": c.benchmark}


async def reset_attempt(db: AsyncSession, sheet: SolveSheet, c: Challenge) -> dict:
    """Reset a challenge's solve record (user-initiated, e.g. to re-practice).
    Deletes the attempt entirely so the challenge goes back to 'not_started'."""
    att = (
        await db.execute(select(Attempt).where(Attempt.sheet_id == sheet.id, Attempt.challenge_id == c.id))
    ).scalar_one_or_none()
    if att is None:
        return {"reset": False, "benchmark": c.benchmark, "note": "no record to reset"}
    # stop running container if any
    if att.status == AttemptStatus.in_progress.value and att.compose_project:
        wd = settings.runs_dir / f"{c.benchmark}_{att.id}"
        try:
            await docker_ops.stop_challenge(att.compose_project, wd)
        except Exception:
            pass
    await db.delete(att)
    await db.commit()
    return {"reset": True, "benchmark": c.benchmark}
