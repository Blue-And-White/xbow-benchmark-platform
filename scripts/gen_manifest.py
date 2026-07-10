#!/usr/bin/env python3
"""
Generate flag_manifest.json for the xbow CTF platform.

For each of the 104 benchmarks under validation-benchmarks/benchmarks/:
  - service:       the docker-compose service whose image receives the FLAG build-arg
                   (resolved by matching the Dockerfile-with-ARG-FLAG's dir to a
                    compose build.context; common.mk passes FLAG via CLI --build-arg,
                    so it's often NOT declared in compose build.args)
  - flag_type:     file | env | embedded
  - flag_path:     absolute path inside the container where the flag ends up
  - flag_line:     raw Dockerfile line that uses $FLAG (debug)
  - original_flag: deterministic FLAG{sha256(UPPER(benchmark_name))} (common.mk default)

Runtime injection (no rebuild):
  - file     -> mount a generated flag file over flag_path (:ro)
  - env      -> environment FLAG=<random> in the per-attempt override
  - embedded -> `docker compose exec -T <svc> sed -i "s|<original_flag>|<random>|g" <flag_path>`
A flag baked into a DB init .sql is marked unsupported (runtime swap unreliable).
"""
from __future__ import annotations

import json
import hashlib
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent / "validation-benchmarks"
BENCH_DIR = REPO / "benchmarks"
OUT = Path(__file__).resolve().parent.parent / "flag_manifest.json"

FLAG_USE_RE = re.compile(r"\$FLAG|\$\{FLAG\}")
WORKDIR_RE = re.compile(r"^\s*WORKDIR\s+([^\s]+)", re.MULTILINE)


def deterministic_flag(benchmark_name: str) -> str:
    h = hashlib.sha256(benchmark_name.upper().encode()).hexdigest()
    return f"FLAG{{{h}}}"


def parse_flag_line(df_text: str) -> tuple[str | None, str | None, str | None]:
    """Return (flag_type, flag_path, flag_line)."""
    workdirs = [m.group(1) for m in WORKDIR_RE.finditer(df_text)]
    workdir = workdirs[-1] if workdirs else "/"
    for raw in df_text.splitlines():
        line = raw.strip()
        if not line.startswith(("RUN", "ENV")):
            continue
        if not FLAG_USE_RE.search(line):
            continue
        # ENV FLAG=...
        if line.startswith("ENV") and re.search(r"\b(FLAG|flag)\b\s*=", line):
            return ("env", None, line)
        # RUN sed ... $FLAG ... <target-file>  (check sed BEFORE the `>` check,
        # since sed expressions contain `>` via `<FLAG>` / `/>` delimiters)
        if "sed" in line:
            toks = line.split()
            if toks:
                path = toks[-1].strip("\"'")
                if not path.startswith("/"):
                    wd = (workdir or "/").rstrip("/")
                    path = f"{wd}/{path}"
                return ("embedded", path, line)
        # RUN ... $FLAG ... > /path   (echo / printf / tee / etc.)
        if ">" in line:
            m = re.search(r'>\s*["\']?([^\s"\'&|;]+)', line)
            if m:
                return ("file", m.group(1).strip("\"'"), line)
    return (None, None, None)


def resolve_services(bench_dir: Path) -> dict:
    """service -> {context(abs), dockerfile, args} via `docker compose config`."""
    compose = bench_dir / "docker-compose.yml"
    if not compose.exists():
        return {}
    try:
        out = subprocess.run(
            ["docker", "compose", "-f", str(compose), "config", "--format", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return {}
        cfg = json.loads(out.stdout)
    except Exception:
        return {}
    services = {}
    for name, svc in cfg.get("services", {}).items():
        build = svc.get("build") or {}
        if isinstance(build, bool):
            build = {}
        ctx = build.get("context")
        df = build.get("dockerfile") or "Dockerfile"
        services[name] = {
            "context": str(Path(ctx).resolve()) if ctx else None,
            "dockerfile": df,
            "args": build.get("args") or {},
        }
    return services


def classify(bench_dir: Path) -> dict:
    name = bench_dir.name
    entry = {
        "benchmark": name, "title": None, "level": None, "tags": [],
        "win_condition": None, "service": None, "flag_type": None,
        "flag_path": None, "flag_line": None, "build_context": None,
        "original_flag": deterministic_flag(name), "supported": False, "note": None,
    }
    bj = bench_dir / "benchmark.json"
    if bj.exists():
        try:
            meta = json.loads(bj.read_text())
            entry["title"] = meta.get("name")
            entry["level"] = str(meta.get("level", ""))
            entry["tags"] = meta.get("tags", [])
            entry["win_condition"] = meta.get("win_condition")
        except Exception as e:
            entry["note"] = f"benchmark.json parse error: {e}"

    services = resolve_services(bench_dir)
    ctx_to_svc = {s["context"]: n for n, s in services.items() if s["context"]}

    # find the Dockerfile that declares ARG FLAG
    flag_df = None
    for df in bench_dir.rglob("Dockerfile"):
        try:
            txt = df.read_text(errors="replace")
        except Exception:
            continue
        if re.search(r"^\s*ARG\s+FLAG\b", txt, re.MULTILINE):
            flag_df = df
            break

    if flag_df is None:
        entry["note"] = (entry["note"] or "") + " no Dockerfile with ARG FLAG."
        return entry

    entry["service"] = ctx_to_svc.get(str(flag_df.parent.resolve())) or "(detected)"
    try:
        entry["build_context"] = str(flag_df.parent.relative_to(bench_dir))
    except ValueError:
        entry["build_context"] = str(flag_df.parent)

    ftype, fpath, fline = parse_flag_line(flag_df.read_text(errors="replace"))
    entry["flag_type"], entry["flag_path"], entry["flag_line"] = ftype, fpath, fline

    if entry["win_condition"] == "question":
        entry["note"] = (entry["note"] or "") + " question-mode; not supported in MVP."
        return entry
    if not ftype:
        entry["note"] = (entry["note"] or "") + " could not detect flag usage."
        return entry
    if entry["service"] == "(detected)":
        entry["note"] = (entry["note"] or "") + " service name unresolved."
    if ftype == "embedded" and fpath and fpath.endswith(".sql"):
        # flag is baked into a mysql init .sql at build time and loaded on the
        # DB's first start; runtime sed won't take effect. Use a FIXED flag
        # (the baked deterministic one) — still recorded + compared on submit.
        entry["flag_type"] = "fixed"
        entry["flag_path"] = fpath
        entry["note"] = (entry["note"] or "") + " DB-init .sql flag -> fixed mode."
        entry["supported"] = True
        return entry
    entry["supported"] = True
    return entry


def main() -> int:
    if not BENCH_DIR.exists():
        print(f"ERROR: {BENCH_DIR} not found. Fetch the repo first.", file=sys.stderr)
        return 1
    benches = sorted(p for p in BENCH_DIR.iterdir() if p.is_dir() and p.name.startswith("XBEN-"))
    print(f"scanning {len(benches)} benchmarks...")
    manifest, unsupported, type_counts, unresolved_svc = [], [], {}, 0
    for b in benches:
        e = classify(b)
        manifest.append(e)
        if e["flag_type"]:
            type_counts[e["flag_type"]] = type_counts.get(e["flag_type"], 0) + 1
        if e["service"] == "(detected)":
            unresolved_svc += 1
        if not e["supported"]:
            unsupported.append(e["benchmark"])
    OUT.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"\nwrote {OUT} ({len(manifest)} entries)")
    print("flag_type counts:", type_counts)
    print(f"supported: {sum(1 for e in manifest if e['supported'])}/{len(manifest)}")
    print(f"service unresolved (detected): {unresolved_svc}")
    if unsupported:
        print(f"unsupported ({len(unsupported)}): {', '.join(unsupported)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
