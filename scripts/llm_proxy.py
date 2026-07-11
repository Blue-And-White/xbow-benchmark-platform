"""Tiny API-key-authed reverse proxy to a local Ollama (OpenAI-compatible).

Listens on a public port; requires `Authorization: Bearer <key>` or
`X-API-Key: <key>`; forwards everything (streaming, SSE-safe) to UPSTREAM
(default http://127.0.0.1:6399, i.e. the local Ollama). So any OpenAI-compatible
client can reach the Ollama models remotely with a key.

  pip install fastapi httpx "uvicorn[standard]"
  LLM_API_KEY=llm_xxx UPSTREAM=http://127.0.0.1:6399 PORT=6888 \
    uvicorn llm_proxy:app --host 0.0.0.0 --port 6888
"""
from __future__ import annotations

import os
import secrets

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

UPSTREAM = os.getenv("UPSTREAM", "http://127.0.0.1:6399").rstrip("/")
API_KEY = os.getenv("LLM_API_KEY") or "llm_" + secrets.token_urlsafe(24)
PORT = int(os.getenv("PORT", "6888"))

_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "content-encoding",
}

app = FastAPI(title="ollama-key-proxy")
print(f"[llm_proxy] API_KEY={API_KEY}  upstream={UPSTREAM}  port={PORT}", flush=True)


def _authorized(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    key = request.headers.get("x-api-key", "")
    return auth == f"Bearer {API_KEY}" or key == API_KEY


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    if not _authorized(request):
        raise HTTPException(401, "invalid api key — use 'Authorization: Bearer <key>' or 'X-API-Key: <key>'")
    target = f"{UPSTREAM}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    client = httpx.AsyncClient(timeout=httpx.Timeout(None), follow_redirects=False)
    req = client.build_request(request.method, target, headers=headers, content=body)
    resp = await client.send(req, stream=True)

    async def gen():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP}
    return StreamingResponse(gen(), status_code=resp.status_code, headers=out_headers)


@app.get("/_proxy_info")
async def info(request: Request):
    # unauthenticated, just shows what model to use + how to auth
    return {"upstream": UPSTREAM, "model_hint": "deepseek-r1:671b", "auth": "Authorization: Bearer <key>"}
