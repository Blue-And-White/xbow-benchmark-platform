"""Reverse proxy: /c/{attempt_id}/... -> the running challenge container.

Public (the attempt_id in the URL is the access token). Only proxies attempts
that are in_progress with a published host port.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import Attempt, AttemptStatus

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
    target = f"http://{settings.challenge_host}:{att.host_port}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"

    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    headers["host"] = f"localhost:{att.host_port}"
    headers["x-forwarded-host"] = request.headers.get("host", "")
    headers["x-forwarded-proto"] = "http"
    headers["x-forwarded-for"] = request.client.host if request.client else ""

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
    return StreamingResponse(gen(), status_code=resp.status_code, headers=out_headers)
