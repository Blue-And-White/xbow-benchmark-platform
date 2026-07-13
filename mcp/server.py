"""MCP server for the xbow CTF platform — lets agents solve challenges via MCP.

Usage (in the agent's MCP config):
  {
    "mcpServers": {
      "xbow-ctf": {
        "command": "python",
        "args": ["mcp/server.py"],
        "env": {
          "XBOW_PLATFORM_URL": "http://121.5.30.191:6888",
          "XBOW_API_KEY": "xben_xxx"
        }
      }
    }
  }

The api-key authenticates to a specific solve-sheet. Tools:
  - list_challenges:  list all 104 challenges + their status (no hints/tags)
  - start_challenge:  start a challenge (returns the reverse-proxy URL)
  - submit_flag:      submit a flag for a running challenge
  - stop_challenge:   manually stop/abandon a running challenge
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

# Minimal MCP stdio server (no external deps beyond httpx).
# Implements JSON-RPC over stdin/stdout per the MCP spec.

PLATFORM_URL = os.getenv("XBOW_PLATFORM_URL", "http://127.0.0.1:6888")
API_KEY = os.getenv("XBOW_API_KEY", "")

_client = httpx.Client(base_url=PLATFORM_URL, timeout=120.0, headers={"X-API-Key": API_KEY})


def _tools() -> list[dict]:
    return [
        {
            "name": "list_challenges",
            "description": "List all 104 xbow challenges and their current status on this solve-sheet. "
                           "Returns: benchmark ID, level, status (not_started/in_progress/abandoned/solved). "
                           "Does NOT reveal tags, titles, or descriptions (no hints).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "start_challenge",
            "description": "Start a challenge by its benchmark ID (e.g. 'XBEN-001-24'). "
                           "Returns the challenge URL (reverse-proxy, publicly accessible). "
                           "Max 3 challenges can run simultaneously per solve-sheet.",
            "inputSchema": {
                "type": "object",
                "properties": {"benchmark": {"type": "string", "description": "Challenge ID, e.g. XBEN-001-24"}},
                "required": ["benchmark"],
            },
        },
        {
            "name": "submit_flag",
            "description": "Submit a flag for a running challenge. Returns correct=true/false. "
                           "If correct, the platform auto-stops and removes the challenge container.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "benchmark": {"type": "string", "description": "Challenge ID, e.g. XBEN-001-24"},
                    "flag": {"type": "string", "description": "The flag to submit, e.g. FLAG{...}"},
                },
                "required": ["benchmark", "flag"],
            },
        },
        {
            "name": "stop_challenge",
            "description": "Manually stop/abandon a running challenge (if you can't solve it). "
                           "Stops the container; the attempt record is kept (status=abandoned).",
            "inputSchema": {
                "type": "object",
                "properties": {"benchmark": {"type": "string", "description": "Challenge ID"}},
                "required": ["benchmark"],
            },
        },
    ]


def _handle_tool(name: str, args: dict) -> str:
    if name == "list_challenges":
        r = _client.get("/api/challenges")
        r.raise_for_status()
        data = r.json()
        lines = [f"{c['benchmark']}  L{c['level']}  {c['status']}" for c in data]
        return f"共 {len(data)} 题:\n" + "\n".join(lines)

    benchmark = args.get("benchmark", "")
    if name == "start_challenge":
        r = _client.post(f"/api/challenges/{benchmark}/start")
        if r.status_code == 200:
            d = r.json()
            return f"已启动 {benchmark}\n靶机地址: {d['url']}\nattempt_id: {d['attempt_id']}"
        return f"启动失败 ({r.status_code}): {r.text}"

    if name == "submit_flag":
        flag = args.get("flag", "")
        r = _client.post(f"/api/challenges/{benchmark}/submit", json={"flag": flag})
        if r.status_code == 200:
            d = r.json()
            if d.get("correct"):
                dur = d.get("solve_duration_ms", 0)
                return f"✅ 正确! {benchmark} 已解 (用时 {dur/1000:.1f}s, 容器已自动关闭)"
            return f"❌ 错误的 flag, 再试试"
        return f"提交失败 ({r.status_code}): {r.text}"

    if name == "stop_challenge":
        r = _client.post(f"/api/challenges/{benchmark}/stop")
        if r.status_code == 200:
            return f"已停止 {benchmark}"
        return f"停止失败 ({r.status_code}): {r.text}"

    return f"unknown tool: {name}"


def _respond(msg_id: Any, result: dict) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
    sys.stdout.flush()


def _error(msg_id: Any, code: int, message: str) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                                 "error": {"code": code, "message": message}}) + "\n")
    sys.stdout.flush()


def main() -> None:
    if not API_KEY:
        sys.stderr.write("ERROR: XBOW_API_KEY env not set\n")
        sys.exit(1)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            _respond(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "xbow-ctf", "version": "0.1.0"},
            })
        elif method == "notifications/initialized":
            pass  # ack, no response needed
        elif method == "tools/list":
            _respond(msg_id, {"tools": _tools()})
        elif method == "tools/call":
            tname = params.get("name", "")
            targs = params.get("arguments", {})
            try:
                output = _handle_tool(tname, targs)
                _respond(msg_id, {"content": [{"type": "text", "text": output}]})
            except Exception as e:
                _respond(msg_id, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
        elif method == "ping":
            _respond(msg_id, {})
        else:
            if msg_id is not None:
                _error(msg_id, -32601, f"method not found: {method}")


if __name__ == "__main__":
    main()
