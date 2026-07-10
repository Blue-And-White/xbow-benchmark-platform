"""FastAPI app: xbow CTF solve-platform."""
from __future__ import annotations

import logging
import secrets

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from . import manifest as manifest_mod
from .config import settings
from .db import SessionLocal, init_db
from .models import Challenge, PlatformConfig, User
from .routers import admin, auth, challenges, leaderboard, proxy, sheets
from .security import hash_password

log = logging.getLogger("xben")

app = FastAPI(title="xbow CTF platform", version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax")

for r in (auth, sheets, challenges, leaderboard, admin, proxy):
    app.include_router(r.router)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.on_event("startup")
async def _startup() -> None:
    await init_db()
    await _seed_config()
    await _seed_admin()
    await _seed_challenges()


async def _seed_config() -> None:
    from sqlalchemy import select
    async with SessionLocal() as db:
        cfg = (await db.execute(select(PlatformConfig).where(PlatformConfig.id == 1))).scalar_one_or_none()
        if cfg is None:
            db.add(PlatformConfig(
                id=1,
                registration_code=settings.registration_code,
                max_concurrent_per_user=settings.max_concurrent_per_user,
                public_base_url=settings.public_base_url,
                allow_direct_port=settings.allow_direct_port,
            ))
            await db.commit()


async def _seed_admin() -> None:
    from sqlalchemy import select
    async with SessionLocal() as db:
        admin = (await db.execute(select(User).where(User.role == "admin"))).scalar_one_or_none()
        if admin is not None:
            return
        pw = settings.admin_password or secrets.token_urlsafe(12)
        db.add(User(username=settings.admin_user, password_hash=hash_password(pw), role="admin"))
        await db.commit()
        log.warning("!!! seeded admin user=%s password=%s (save it, shown once)", settings.admin_user, pw)


async def _seed_challenges() -> None:
    from sqlalchemy import select
    async with SessionLocal() as db:
        existing = {b for (b,) in (await db.execute(select(Challenge.benchmark))).all()}
        added = 0
        for e in manifest_mod.manifest_entries():
            if e["benchmark"] in existing:
                continue
            db.add(Challenge(
                benchmark=e["benchmark"],
                title=e.get("title"),
                level=e.get("level"),
                tags=",".join(e.get("tags") or []),
                win_condition=e.get("win_condition") or "flag",
                supported=bool(e.get("supported")),
                service=e.get("service"),
                flag_type=e.get("flag_type"),
                flag_path=e.get("flag_path"),
                original_flag=e.get("original_flag"),
            ))
            added += 1
        if added:
            await db.commit()
        log.info("challenges seeded: %d added (%d total)", added, len(existing) + added)
