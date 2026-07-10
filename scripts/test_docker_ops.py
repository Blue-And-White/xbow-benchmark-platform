"""End-to-end smoke test for app.docker_ops against built benchmarks.

Validates that start_challenge injects the random per-attempt flag and the app
serves HTTP 200, for both file (XBEN-001-24) and embedded (XBEN-024-24) flag types.
Run: .venv/bin/python scripts/test_docker_ops.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid

sys.path.insert(0, ".")  # so `from app import ...` works when run as a script

from app import docker_ops, manifest

CASES = ["XBEN-001-24", "XBEN-024-24"]


def read_in_container(project: str, work_dir, service: str, path: str) -> str:
    cmd = ["docker", "compose", "-p", project, "-f", str(work_dir / "compose.yml"),
           "exec", "-T", service, "cat", path]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return p.stdout.strip()


def grep_in_container(project: str, work_dir, service: str, needle: str, path: str) -> bool:
    cmd = ["docker", "compose", "-p", project, "-f", str(work_dir / "compose.yml"),
           "exec", "-T", service, "grep", "-c", needle, path]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return p.returncode == 0 and int(p.stdout.strip() or "0") > 0


async def test_one(benchmark: str) -> bool:
    m = manifest.get(benchmark)
    assert m and m["supported"], f"{benchmark} not supported"
    rand = f"FLAG{{smoketest-{uuid.uuid4().hex[:12]}}}"
    print(f"\n=== {benchmark} (type={m['flag_type']} svc={m['service']} path={m['flag_path']}) ===")
    print(f"random flag: {rand}")
    attempt_id = abs(hash(benchmark + rand)) % 10_000_000

    inst = await docker_ops.start_challenge(benchmark, attempt_id, rand)
    print(f"started: project={inst.project} port={inst.host_port}")

    ok_flag = False
    if m["flag_type"] == "file":
        got = read_in_container(inst.project, inst.work_dir, m["service"], m["flag_path"])
        ok_flag = (got == rand)
        print(f"container flag file: {got!r}  -> {'MATCH' if ok_flag else 'MISMATCH'}")
    elif m["flag_type"] == "embedded":
        has_rand = grep_in_container(inst.project, inst.work_dir, m["service"], rand, m["flag_path"])
        has_orig = grep_in_container(inst.project, inst.work_dir, m["service"], m["original_flag"], m["flag_path"])
        ok_flag = has_rand and not has_orig
        print(f"embedded: random present={has_rand} original present={has_orig} -> {'MATCH' if ok_flag else 'MISMATCH'}")

    # app reachable
    import urllib.request
    url = f"http://localhost:{inst.host_port}/"
    try:
        code = urllib.request.urlopen(url, timeout=15).getcode()
    except Exception as e:
        code = f"ERR {e}"
    print(f"app {url} -> {code}")
    await docker_ops.stop_challenge(inst.project, inst.work_dir)
    print("stopped.")
    return ok_flag and code == 200


async def main() -> int:
    results = {}
    for b in CASES:
        try:
            results[b] = await test_one(b)
        except Exception as e:
            print(f"!!! {b} raised: {e}")
            results[b] = False
    print("\n================ RESULT ================")
    for b, ok in results.items():
        print(f"  {b}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.path.insert(0, ".")
    raise SystemExit(asyncio.run(main()))
