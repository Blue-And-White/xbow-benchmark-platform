"""Reverse proxy: /c/{attempt_id}/... -> the running challenge container.

Public (the attempt_id in the URL is the access token). Only proxies attempts
that are in_progress with a published host port.

For challenges with proxy_prefix (e.g. XBEN-084 with basePath="/app"):
  /c/{attempt_id}/xxx  ->  strip /c/{id}/  ->  add /app/  ->  /app/xxx
This allows Next.js apps with basePath to work under path-prefix proxy.
"""
from __future__ import annotations

import re as _re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import Attempt, AttemptStatus, Challenge

router = APIRouter(tags=["proxy"])

_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade", "host",
        # NOTE: content-encoding is NOT stripped — if the challenge returns gzip,
        # we pass it through with the header so the browser can decompress.
        # (stripping it causes garbled content: compressed bytes + no header)
        "content-length"}


@router.api_route("/c/{attempt_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy(attempt_id: int, path: str, request: Request, db: AsyncSession = Depends(get_db)):
    att = (await db.execute(select(Attempt).where(Attempt.id == attempt_id))).scalar_one_or_none()
    if not att or att.status != AttemptStatus.in_progress.value or not att.host_port:
        raise HTTPException(404, "challenge not running")

    # Lookup challenge for proxy_prefix (e.g. "/app" for XBEN-084 basePath)
    ch = (await db.execute(select(Challenge).where(Challenge.id == att.challenge_id))).scalar_one_or_none()
    prefix = (ch.proxy_prefix or "").rstrip("/")  # "/app" or ""

    # Build target path: strip /c/{id}/ then add proxy_prefix
    # Normal:  /c/{id}/xxx  -> /xxx
    # Prefixed: /c/{id}/xxx -> /app/xxx
    # Prefixed root: /c/{id}/  -> /app  (not /app/ to avoid double-slash)
    if prefix:
        target_path = f"/{prefix}/{path}" if path else f"/{prefix}"
    else:
        target_path = f"/{path}" if path else "/"

    target = f"http://{settings.challenge_host}:{att.host_port}{target_path}"
    if request.url.query:
        target += f"?{request.url.query}"

    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    headers["host"] = f"localhost:{att.host_port}"
    headers["x-forwarded-host"] = request.headers.get("host", "")
    headers["x-forwarded-proto"] = "http"
    headers["x-forwarded-for"] = request.client.host if request.client else ""
    # Tell the challenge app it's behind a path-prefix proxy
    if prefix:
        headers["x-forwarded-prefix"] = prefix

    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=False)
    req = client.build_request(request.method, target, headers=headers, content=body)
    resp = await client.send(req, stream=True)

    async def gen():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    # strip hop-by-hop + content-encoding (httpx already decoded the body)
    out_headers = {k: v for k, v in resp.headers.items()
                   if k.lower() not in _HOP and k.lower() != "content-encoding" and k.lower() != "content-length"}

    # For prefixed challenges: rewrite redirect Location headers.
    # Next.js with basePath="/app" sends Location like:
    #   - "/app/login" -> rewrite to "/c/{id}/login" (strip /app prefix)
    #   - "http://localhost:33177/app/login" -> rewrite to "/c/{id}/login"
    # This keeps the browser within our proxy instead of going to the
    # internal container URL.
    if prefix and "location" in out_headers:
        loc = out_headers["location"]
        proxy_path = f"/c/{attempt_id}"
        # Absolute URL: http://host:port/app/xxx -> /c/{id}/xxx
        new_loc = _re.sub(
            rf"https?://[^/]+/{prefix}(/.*|$)",
            rf"{proxy_path}\1",
            loc,
        )
        # Relative URL: /app/xxx -> /c/{id}/xxx
        if loc.startswith(f"/{prefix}") or loc.startswith(f"/{prefix}/"):
            rest = loc[len(prefix) + 1:]  # strip "/app" prefix, keep rest
            new_loc = f"{proxy_path}{rest}"
        out_headers["location"] = new_loc

    return StreamingResponse(gen(), status_code=resp.status_code, headers=out_headers)
