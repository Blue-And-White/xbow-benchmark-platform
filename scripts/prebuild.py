#!/usr/bin/env python3
"""Pre-build xbow benchmark images (run on the host that runs the platform).

For each benchmark (first --n of them): find the Dockerfile declaring ARG FLAG,
copy the host CA bundle into its build context, patch the Dockerfile
(host CA + CN apt/apk/pip mirrors — avoids build hangs on China/throttled nets),
run `make build` (common.mk injects the deterministic flag), then restore the
Dockerfile and remove the CA bundle. Logs per-benchmark ok/fail/skip; continues
on failure. Idempotent: benchmarks whose guard file exists (already built) are
skipped by make. Run in background on slow (vfs) hosts.

  python3 scripts/prebuild.py --repo /root/validation-benchmarks --n 60 --log /root/prebuild.log
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

CA_PATH = "/etc/ssl/certs/ca-certificates.crt"

# inserted right after the first FROM line. Codename-aware:
# - EOL debian (buster/stretch/jessie/wheezy) -> archive.debian.org (deb.debian.org
#   dropped them; Tencent mirror doesn't keep them either) and drop -updates.
# - current debian -> tencent mirror; ubuntu -> tencent; alpine -> aliyun.
PATCH = (
    "COPY ca-certificates.crt /etc/ssl/certs/ca-certificates.crt\n"
    "RUN echo 'Acquire::Retries \"20\"; Acquire::http::Proxy \"http://172.17.0.1:3142\"; Acquire::https::Proxy \"http://172.17.0.1:3142\";' > /etc/apt/apt.conf.d/80proxy; \\\n"
    "    if grep -qE ' (buster|stretch|jessie|wheezy)( |$|-|/)' /etc/apt/sources.list 2>/dev/null; then \\\n"
    "      sed -i 's|deb.debian.org|archive.debian.org|g; s|security.debian.org|archive.debian.org|g' /etc/apt/sources.list; \\\n"
    "      sed -i '/-updates/d; /snapshot.debian.org/d' /etc/apt/sources.list; \\\n"
    "    else \\\n"
    "      sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g; s|security.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list 2>/dev/null; \\\n"
    "      sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g; s|security.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list.d/*.sources 2>/dev/null; \\\n"
    "      sed -i 's|archive.ubuntu.com|mirrors.cloud.tencent.com|g; s|security.ubuntu.com|mirrors.cloud.tencent.com|g' /etc/apt/sources.list 2>/dev/null; \\\n"
    "    fi; \\\n"
    "    sed -i 's|dl-cdn.alpinelinux.org|mirrors.aliyun.com|g' /etc/apk/repositories 2>/dev/null; true\n"
    "ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple PIP_DISABLE_PIP_VERSION_CHECK=1 \\\n"
    "    npm_config_registry=https://registry.npmmirror.com COMPOSER_HOME=/tmp/composer\n"
    "RUN mkdir -p /tmp/composer && printf '{\"repositories\":{\"packagist.org\":{\"type\":\"composer\",\"url\":\"https://mirrors.aliyun.com/composer/\"}}}\\n' > /tmp/composer/config.json && chmod 644 /tmp/composer/config.json; true\n"
)


def find_flag_dockerfile(bench_dir: Path) -> Path | None:
    for df in sorted(bench_dir.rglob("Dockerfile")):
        try:
            txt = df.read_text(errors="replace")
        except Exception:
            continue
        if re.search(r"^\s*ARG\s+FLAG\b", txt, re.MULTILINE):
            return df
    return None


PHANTOMJS_URL = "https://bitbucket.org/ariya/phantomjs/downloads/phantomjs-2.1.1-linux-x86_64.tar.bz2"


def _ensure_phantomjs(cache_path: Path) -> bool:
    """Download phantomjs binary once (bitbucket ~22KB/s from China, ~17min for 23MB)."""
    if cache_path.exists() and cache_path.stat().st_size > 23_000_000:
        return True
    p = subprocess.run(["curl", "-fsSL", "-C", "-", "--retry", "20", "--retry-delay", "5",
                        "--max-time", "1800", "-o", str(cache_path), PHANTOMJS_URL],
                       capture_output=True, timeout=1860)
    return p.returncode == 0 and cache_path.exists() and cache_path.stat().st_size > 23_000_000


def insert_patch(text: str, extra: str = "") -> str:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("FROM "):
            blocks = [PATCH] + ([extra] if extra else [])
            return "\n".join(lines[: i + 1] + blocks + lines[i + 1:]) + "\n"
    return text


def build_one(bench_dir: Path, ca_bytes: bytes, timeout: int = 3600) -> str:
    df = find_flag_dockerfile(bench_dir)
    if df is None:
        return "skip(no ARG FLAG Dockerfile)"
    ctx = df.parent
    ca = ctx / "ca-certificates.crt"
    orig = df.read_text()
    try:
        ca.write_bytes(ca_bytes)
        is_phantomjs = "phantomjs" in orig.lower()
        if is_phantomjs:
            pj_cache = Path("/root/phantomjs.tar.bz2")
            if not _ensure_phantomjs(pj_cache):
                return "FAIL(cant download phantomjs binary)"
            pj_ctx = ctx / "phantomjs.tar.bz2"
            shutil.copy2(str(pj_cache), str(pj_ctx))  # copy (keep cache, no re-download)
            pj_block = (
                "RUN apt-get update && apt-get install -y --no-install-recommends bzip2\n"
                "COPY phantomjs.tar.bz2 /tmp/phantomjs.tar.bz2\n"
                "RUN tar xjf /tmp/phantomjs.tar.bz2 -C /usr/local --strip-components=2 "
                "phantomjs-2.1.1-linux-x86_64/bin/phantomjs && rm /tmp/phantomjs.tar.bz2 "
                "&& ln -sf /usr/local/phantomjs /usr/local/bin/phantomjs\n"
            )
            patched = insert_patch(orig, extra=pj_block)
            patched = patched.replace("apt-get install -y phantomjs",
                                      "apt-get install -y --no-install-recommends libfontconfig1 libfreetype6 libssl1.1 bzip2")
        else:
            patched = insert_patch(orig)
        df.write_text(patched)
        # 'make clean' removes the stale .xben_build_done guard so make actually
        # rebuilds (the guard can outlive a wiped docker data dir -> false skip).
        p = subprocess.run(["bash", "-c", "make clean && make build"], cwd=bench_dir,
                           capture_output=True, text=True, timeout=timeout)
        if p.returncode == 0:
            return "ok"
        tail = (p.stderr or p.stdout or "")[-400:].replace("\n", " ")
        return f"FAIL(rc={p.returncode}): {tail}"
    except subprocess.TimeoutExpired:
        return "FAIL(timeout)"
    except Exception as e:
        return f"FAIL(exc): {e}"
    finally:
        try:
            df.write_text(orig)
            if ca.exists():
                ca.unlink()
            pj_ctx = ctx / "phantomjs.tar.bz2"
            if pj_ctx.exists():
                pj_ctx.unlink()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="/root/validation-benchmarks")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--start", type=int, default=0, help="skip first N benchmarks (0-indexed)")
    ap.add_argument("--only", default="", help="comma-separated benchmark names to build (overrides start/n)")
    ap.add_argument("--log", default="/root/prebuild.log")
    ap.add_argument("--ca", default=CA_PATH)
    args = ap.parse_args()

    bdir = Path(args.repo) / "benchmarks"
    if not bdir.exists():
        print(f"ERROR: {bdir} not found", file=sys.stderr)
        return 1
    ca_path = Path(args.ca)
    if not ca_path.exists():
        print(f"ERROR: CA bundle {ca_path} not found", file=sys.stderr)
        return 1
    ca_bytes = ca_path.read_bytes()

    # ensure dockerd is up
    info = subprocess.run(["docker", "info"], capture_output=True)
    if info.returncode != 0:
        print("ERROR: docker daemon not reachable; start dockerd first", file=sys.stderr)
        return 1

    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        benches = [p for p in sorted(bdir.iterdir()) if p.is_dir() and p.name in want]
    else:
        benches = sorted(p for p in bdir.iterdir() if p.is_dir() and p.name.startswith("XBEN-"))[args.start: args.start + args.n]
    ok = fail = skip = 0
    with open(args.log, "a") as log:
        log.write(f"\n=== prebuild start {time.strftime('%F %T')} n={len(benches)} repo={args.repo} ===\n")
        log.flush()
        for b in benches:
            t = time.time()
            res = build_one(b, ca_bytes)
            dt = time.time() - t
            line = f"{b.name}: {res} ({dt:.0f}s)"
            log.write(line + "\n")
            log.flush()
            print(line, flush=True)
            if res == "ok":
                ok += 1
            elif res.startswith("skip"):
                skip += 1
            else:
                fail += 1
        summary = f"=== done {time.strftime('%F %T')}: ok={ok} fail={fail} skip={skip} (of {len(benches)}) ==="
        log.write(summary + "\n")
        print(summary, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
