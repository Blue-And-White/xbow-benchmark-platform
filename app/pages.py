"""HTML pages (session-authenticated) — humans use the UI; agents use the /api (api-key) endpoints.

Both call into app.service for the shared start/submit/stop logic.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .deps import current_user, get_config
from .models import Attempt, Challenge, SolveSheet, User
from .security import generate_api_key, hash_password, verify_password
from .service import ServiceError, delete_sheet as svc_delete_sheet, start as svc_start, stop as svc_stop, submit as svc_submit

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(tags=["pages"])


def _elapsed(att) -> int:
    if not att or not att.started_at:
        return 0
    s = att.started_at
    if s.tzinfo is None:
        s = s.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - s).total_seconds())


templates.env.globals["elapsed"] = _elapsed


# ----------------------------- helpers ----------------------------- #
async def _sheet_for(db: AsyncSession, user: User, sheet_id: int) -> SolveSheet:
    s = (await db.execute(select(SolveSheet).where(SolveSheet.id == sheet_id, SolveSheet.user_id == user.id))).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "sheet not found")
    return s


async def _chall(db: AsyncSession, benchmark: str) -> Challenge:
    c = (await db.execute(select(Challenge).where(Challenge.benchmark == benchmark))).scalar_one_or_none()
    if c is None:
        raise HTTPException(404, "no such challenge")
    return c


async def _atts_map(db: AsyncSession, sheet_id: int) -> dict[int, Attempt]:
    return {a.challenge_id: a for a in
            (await db.execute(select(Attempt).where(Attempt.sheet_id == sheet_id))).scalars().all()}


def _status_of(c: Challenge, atts: dict[int, Attempt]) -> str:
    a = atts.get(c.id)
    if a and a.status == "solved":
        return "solved"
    if a and a.status == "in_progress":
        return "in_progress"
    return "not_started"


def _stats(challs: list[Challenge], atts: dict[int, Attempt]) -> dict:
    solved = running = 0
    levels: dict[str, dict] = {}
    for c in challs:
        st = _status_of(c, atts)
        if st == "solved":
            solved += 1
        elif st == "in_progress":
            running += 1
        lv = c.level or "?"
        d = levels.setdefault(lv, {"total": 0, "solved": 0, "running": 0})
        d["total"] += 1
        if st == "solved":
            d["solved"] += 1
        elif st == "in_progress":
            d["running"] += 1
    total = len(challs)
    return {
        "total": total, "solved": solved, "running": running,
        "unsolved": total - solved, "levels": levels,
    }


async def _board_ctx(db: AsyncSession, sheet: SolveSheet, user: User) -> dict:
    challs = (await db.execute(select(Challenge).order_by(Challenge.benchmark))).scalars().all()
    atts = await _atts_map(db, sheet.id)
    return {
        "sheet": sheet, "challenges": challs, "atts": atts,
        "cfg": await get_config(db), "user": user, "stats": _stats(challs, atts),
    }


# ----------------------------- auth pages ----------------------------- #
@router.get("/", include_in_schema=False)
async def index(request: Request) -> RedirectResponse:
    return RedirectResponse("/sheets" if request.session.get("user_id") else "/login", status_code=303)


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, error: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": error})


@router.post("/login", include_in_schema=False)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                       db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(request, "login.html", {"error": "用户名或密码错误"})
    request.session["user_id"] = user.id
    return RedirectResponse("/sheets", status_code=303)


@router.get("/register", response_class=HTMLResponse, include_in_schema=False)
async def register_page(request: Request, error: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "register.html", {"error": error})


@router.post("/register", include_in_schema=False)
async def register_submit(request: Request, registration_code: str = Form(...),
                          username: str = Form(...), password: str = Form(...),
                          db: AsyncSession = Depends(get_db)):
    cfg = await get_config(db)
    if registration_code != cfg.registration_code:
        return templates.TemplateResponse(request, "register.html", {"error": "注册码错误"})
    if (await db.execute(select(User).where(User.username == username))).scalar_one_or_none():
        return templates.TemplateResponse(request, "register.html", {"error": "用户名已存在"})
    db.add(User(username=username, password_hash=hash_password(password)))
    await db.commit()
    return RedirectResponse("/login?error=注册成功，请登录", status_code=303)


@router.post("/logout", include_in_schema=False)
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ----------------------------- sheets ----------------------------- #
@router.get("/sheets", response_class=HTMLResponse, include_in_schema=False)
async def sheets_page(request: Request, user: User = Depends(current_user),
                      db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(SolveSheet).where(SolveSheet.user_id == user.id).order_by(SolveSheet.id))).scalars().all()
    challs = (await db.execute(select(Challenge).order_by(Challenge.benchmark))).scalars().all()
    atts_by_sheet: dict[int, dict[int, Attempt]] = {}
    if rows:
        for a in (await db.execute(select(Attempt).where(Attempt.sheet_id.in_([s.id for s in rows])))).scalars().all():
            atts_by_sheet.setdefault(a.sheet_id, {})[a.challenge_id] = a
    sheet_stats = {s.id: _stats(challs, atts_by_sheet.get(s.id, {})) for s in rows}
    return templates.TemplateResponse(request, "sheets.html", {"user": user, "sheets": rows, "sheet_stats": sheet_stats})


@router.post("/sheets", include_in_schema=False)
async def sheets_create(request: Request, name: str = Form(...), user: User = Depends(current_user),
                        db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    sheet = SolveSheet(user_id=user.id, name=(name.strip() or "sheet")[:128], api_key=generate_api_key())
    db.add(sheet)
    await db.commit()
    return RedirectResponse("/sheets", status_code=303)


@router.post("/sheets/{sheet_id}/delete", include_in_schema=False)
async def sheets_delete(sheet_id: int, user: User = Depends(current_user),
                        db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    sheet = await _sheet_for(db, user, sheet_id)
    await svc_delete_sheet(db, sheet)
    return RedirectResponse("/sheets", status_code=303)


@router.get("/sheets/{sheet_id}", response_class=HTMLResponse, include_in_schema=False)
async def board_page(request: Request, sheet_id: int, user: User = Depends(current_user),
                     db: AsyncSession = Depends(get_db)):
    sheet = await _sheet_for(db, user, sheet_id)
    return templates.TemplateResponse(request, "board.html", await _board_ctx(db, sheet, user))


@router.get("/sheets/{sheet_id}/board", response_class=HTMLResponse, include_in_schema=False)
async def board_partial(request: Request, sheet_id: int, user: User = Depends(current_user),
                        db: AsyncSession = Depends(get_db)):
    sheet = await _sheet_for(db, user, sheet_id)
    return templates.TemplateResponse(request, "_board.html", await _board_ctx(db, sheet, user))


# ----------------------------- HTMX row actions ----------------------------- #
async def _render_row(request, db, sheet, c, cfg):
    atts = await _atts_map(db, sheet.id)
    return templates.TemplateResponse(request, "_row.html", {"sheet": sheet, "c": c, "att": atts.get(c.id), "cfg": cfg})


@router.post("/sheets/{sheet_id}/challenges/{benchmark}/start", response_class=HTMLResponse)
async def row_start(request: Request, sheet_id: int, benchmark: str, user: User = Depends(current_user),
                    db: AsyncSession = Depends(get_db), cfg=Depends(get_config)):
    sheet = await _sheet_for(db, user, sheet_id)
    c = await _chall(db, benchmark)
    try:
        await svc_start(db, sheet, c, cfg)
    except ServiceError as e:
        raise HTTPException(e.status_code, e.detail)
    return await _render_row(request, db, sheet, c, cfg)


@router.post("/sheets/{sheet_id}/challenges/{benchmark}/submit", response_class=HTMLResponse)
async def row_submit(request: Request, sheet_id: int, benchmark: str, flag: str = Form(...),
                     user: User = Depends(current_user), db: AsyncSession = Depends(get_db), cfg=Depends(get_config)):
    sheet = await _sheet_for(db, user, sheet_id)
    c = await _chall(db, benchmark)
    try:
        await svc_submit(db, sheet, c, flag)
    except ServiceError as e:
        raise HTTPException(e.status_code, e.detail)
    return await _render_row(request, db, sheet, c, cfg)


@router.post("/sheets/{sheet_id}/challenges/{benchmark}/stop", response_class=HTMLResponse)
async def row_stop(request: Request, sheet_id: int, benchmark: str, user: User = Depends(current_user),
                   db: AsyncSession = Depends(get_db), cfg=Depends(get_config)):
    sheet = await _sheet_for(db, user, sheet_id)
    c = await _chall(db, benchmark)
    await svc_stop(db, sheet, c)
    return await _render_row(request, db, sheet, c, cfg)


# ----------------------------- api help ----------------------------- #
@router.get("/api-help", response_class=HTMLResponse, include_in_schema=False)
async def api_help(request: Request, user: User = Depends(current_user),
                   db: AsyncSession = Depends(get_db)):
    sheets = (await db.execute(select(SolveSheet).where(SolveSheet.user_id == user.id).order_by(SolveSheet.id))).scalars().all()
    return templates.TemplateResponse(request, "api_help.html", {"user": user, "sheets": sheets})


# ----------------------------- leaderboard / admin ----------------------------- #
@router.get("/leaderboard", response_class=HTMLResponse, include_in_schema=False)
async def leaderboard_page(request: Request, db: AsyncSession = Depends(get_db)):
    from .models import AttemptStatus
    from sqlalchemy import func
    stmt = (
        select(User.username, func.count(Attempt.id), func.coalesce(func.sum(Attempt.solve_duration_ms), 0))
        .join(SolveSheet, SolveSheet.user_id == User.id)
        .join(Attempt, Attempt.sheet_id == SolveSheet.id)
        .where(Attempt.status == AttemptStatus.solved.value)
        .group_by(User.id, User.username)
        .order_by(func.count(Attempt.id).desc(), func.sum(Attempt.solve_duration_ms).asc())
    )
    rows = (await db.execute(stmt)).all()
    board = [{"rank": i + 1, "username": r[0], "solved": int(r[1]), "total_ms": int(r[2])} for i, r in enumerate(rows)]
    return templates.TemplateResponse(request, "leaderboard.html", {"board": board})


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_page(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    cfg = await get_config(db)
    users = (await db.execute(select(User).order_by(User.id))).scalars().all()
    return templates.TemplateResponse(request, "admin.html", {"user": user, "cfg": cfg, "users": users})


@router.post("/admin", include_in_schema=False)
async def admin_update(request: Request, registration_code: str = Form(None), max_concurrent: int = Form(None),
                       public_base_url: str = Form(None), allow_direct_port: str = Form(None),
                       user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    cfg = await get_config(db)
    if registration_code is not None:
        cfg.registration_code = registration_code
    if max_concurrent is not None:
        cfg.max_concurrent_per_user = max_concurrent
    if public_base_url is not None:
        cfg.public_base_url = public_base_url
    if allow_direct_port is not None:
        cfg.allow_direct_port = allow_direct_port == "on"
    await db.commit()
    return RedirectResponse("/admin", status_code=303)
