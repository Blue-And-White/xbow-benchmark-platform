"""FastAPI app: xbow CTF solve-platform."""
from __future__ import annotations

import logging
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from . import manifest as manifest_mod
from . import pages
from .config import settings
from .db import SessionLocal, init_db
from .models import Challenge, PlatformConfig, User
from .routers import admin, auth, challenges, leaderboard, proxy, sheets
from .security import hash_password

log = logging.getLogger("xben")

app = FastAPI(
    title="xbow CTF platform", version="0.1.0",
    docs_url=None,        # disable /docs in production
    redoc_url=None,       # disable /redoc
    openapi_url=None,     # disable /openapi.json (don't leak API structure)
)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax")

for r in (auth, sheets, challenges, leaderboard, admin):
    app.include_router(r.router, prefix="/api")   # JSON / api-key API
app.include_router(proxy.router)                 # /c/{id}/ public challenge proxy
app.include_router(pages.router)                 # HTML UI at /


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """For browser requests (HTML), redirect to /login on 401/403.
    For API requests (JSON), return the normal JSON error."""
    accept = request.headers.get("accept", "")
    if exc.status_code in (401, 403) and "text/html" in accept:
        location = exc.headers.get("Location", "/login") if exc.headers else "/login"
        return RedirectResponse(location, status_code=303)
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


@app.on_event("startup")
async def _startup() -> None:
    await init_db()
    await _seed_config()
    await _seed_admin()
    await _seed_challenges()
    await _refresh_image_status()  # initial sync on startup
    import asyncio
    asyncio.create_task(_image_refresh_loop())  # background 5-min refresh


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
                auto_refresh_images=True,
            ))
            await db.commit()


async def _refresh_image_status() -> None:
    """Check docker images for all challenges and update image_built in DB."""
    from . import docker_ops
    from sqlalchemy import select
    async with SessionLocal() as db:
        challs = (await db.execute(select(Challenge))).scalars().all()
        for c in challs:
            if c.service:
                c.image_built = await docker_ops.image_exists(c.benchmark, c.service)
            else:
                c.image_built = False
        await db.commit()
        built = sum(1 for c in challs if c.image_built)
        log.info("image status refreshed: %d/%d built", built, len(challs))


async def _image_refresh_loop() -> None:
    """Background task: refresh image status every 5 minutes if auto_refresh_images is on."""
    import asyncio
    from sqlalchemy import select
    while True:
        await asyncio.sleep(300)  # 5 minutes
        try:
            async with SessionLocal() as db:
                cfg = (await db.execute(select(PlatformConfig).where(PlatformConfig.id == 1))).scalar_one_or_none()
                if cfg and not cfg.auto_refresh_images:
                    continue
            await _refresh_image_status()
        except Exception as e:
            log.warning("image refresh error: %s", e)


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
