"""
Docker-compose orchestration for per-attempt challenge instances.

Each attempt gets:
  - a unique compose project name (xben_<attempt_id>)
  - a temp work dir with a NORMALIZED + MERGED compose.yml:
        * build.context made absolute (so it works from the work dir)
        * expose "X:Y" -> "X" (compose v5 rejects the malformed X:Y form)
        * flag injection per manifest flag_type (file mount / env)
        * GOMAXPROCS set on every service (kills old-Go panic on big-core hosts)
  - file-type: a generated flag file mounted over flag_path
  - embedded: after `up`, exec sed to replace original_flag -> dynamic_flag in flag_path
"""
from __future__ import annotations

import asyncio
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import settings
from .manifest import get as get_manifest


@dataclass
class ChallengeInstance:
    project: str
    host_port: int | None          # primary (HTTP) host port for proxy
    extra_ports: dict | None       # {container_port: host_port} for non-HTTP ports (e.g. SSH)
    work_dir: Path


def _norm_expose(val):
    """normalize an `expose` item: '3306:3306' -> '3306' (container port)."""
    if isinstance(val, (int, float)):
        return str(val)
    s = str(val).strip()
    if ":" in s:
        # take the container (second) part; for the common equal case it's the same
        s = s.split(":")[-1]
    return s.strip("'\"")


def _randomize_port(p):
    """Convert fixed host:container port mappings to container-only format.
    '5003:5003' -> '5003'  (Docker assigns random host port, avoids conflicts)
    '5003'      -> '5003'  (already ephemeral, no change)
    '8080:80'   -> '80'    (keep only container port)
    dict form   -> dict with published removed (Docker picks random)
    int         -> int     (already ephemeral)
    """
    if isinstance(p, int):
        return p
    if isinstance(p, str):
        s = p.strip()
        if ":" in s:
            # "HOST:CONTAINER" or "HOST:CONTAINER/PROTO" — keep container part only
            parts = s.split(":")
            container = parts[-1]  # e.g. "5003" or "5003/tcp"
            return container
        # already just a container port (ephemeral), keep as-is
        return s
    if isinstance(p, dict):
        # Remove fixed 'published' to let Docker assign randomly
        # Keep 'target' (container port) and other metadata
        out = {k: v for k, v in p.items() if k != "published"}
        return out
    return p


def _build_compose(benchmark: str, dynamic_flag: str, work_dir: Path) -> tuple[dict, str | None]:
    """Return (merged compose dict, flag_service_name) written to work_dir/compose.yml."""
    bench_dir = settings.benchmarks_dir / benchmark
    src_compose = bench_dir / "docker-compose.yml"
    cfg = yaml.safe_load(src_compose.read_text()) or {}
    services: dict = cfg.setdefault("services", {})

    manifest = get_manifest(benchmark) or {}
    flag_service = manifest.get("service") or ""
    flag_type = manifest.get("flag_type")
    flag_path = manifest.get("flag_path")

    # if the manifest service isn't a real compose service, try to detect it
    if flag_service not in services or not flag_service:
        flag_service = _detect_flag_service(services, bench_dir) or next(iter(services), "")

    gmp = str(settings.flag_gomaxprocs)

    for sname, svc in services.items():
        # pin to the PRE-BUILT image name ONLY for services that have a 'build'
        build = svc.get("build")
        if build is not None:
            svc["image"] = f"{benchmark.lower()}-{sname}"
        # absolute build context (build can be a string "./mysql" or a dict)
        if isinstance(build, str):
            build = {"context": build}
            svc["build"] = build
        if isinstance(build, dict):
            ctx = build.get("context")
            if ctx:
                build["context"] = str((bench_dir / ctx).resolve())
        # normalize expose
        if "expose" in svc:
            svc["expose"] = [_norm_expose(v) for v in (svc["expose"] or [])]
        # GOMAXPROCS on every service
        env = svc.setdefault("environment", [])
        if isinstance(env, dict):
            env.setdefault("GOMAXPROCS", gmp)
        elif isinstance(env, list):
            if not any(str(x).startswith("GOMAXPROCS=") for x in env):
                env.append(f"GOMAXPROCS={gmp}")
        # randomize fixed host port mappings -> avoid conflicts when running
        # multiple challenges concurrently (e.g. 7 challenges all on port 5003)
        ports = svc.get("ports") or []
        svc["ports"] = [_randomize_port(p) for p in ports]

    # flag injection on the flag service
    if flag_service and flag_service in services and flag_type:
        svc = services[flag_service]
        if flag_type == "file" and flag_path:
            flag_file = work_dir / "flag.txt"
            flag_file.write_text(dynamic_flag)
            vols = svc.setdefault("volumes", [])
            vols.append(f"{flag_file.resolve()}:{flag_path}:ro")
        elif flag_type == "env":
            env = svc.setdefault("environment", [])
            if isinstance(env, dict):
                env["FLAG"] = dynamic_flag
                env["flag"] = dynamic_flag
            elif isinstance(env, list):
                env.append(f"FLAG={dynamic_flag}")
        # embedded: handled post-up via exec sed

    (work_dir / "compose.yml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    return cfg, flag_service


def _detect_flag_service(services: dict, bench_dir: Path) -> str | None:
    """Fallback: find the service whose build context Dockerfile declares ARG FLAG."""
    for sname, svc in services.items():
        build = svc.get("build")
        if not isinstance(build, dict):
            continue
        ctx = build.get("context") or "."
        df = build.get("dockerfile") or "Dockerfile"
        dfp = (bench_dir / ctx / df) if ctx else (bench_dir / df)
        if dfp.exists():
            try:
                txt = dfp.read_text(errors="replace")
            except Exception:
                continue
            if re.search(r"^\s*ARG\s+FLAG\b", txt, re.MULTILINE):
                return sname
    return None


async def _run(cmd: list[str], timeout: int | None = None) -> tuple[int, str, str]:
    """Run a subprocess in a worker thread, return (rc, stdout, stderr)."""
    def _go() -> tuple[int, str, str]:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    return await asyncio.to_thread(_go)


def image_name(benchmark: str, service: str) -> str:
    return f"{benchmark.lower()}-{service}"


async def image_exists(benchmark: str, service: str) -> bool:
    rc, _, _ = await _run(["docker", "image", "inspect", image_name(benchmark, service)])
    return rc == 0


def _compose_base(project: str, work_dir: Path) -> list[str]:
    return ["docker", "compose", "-p", project, "-f", str(work_dir / "compose.yml")]


async def start_challenge(benchmark: str, attempt_id: int, dynamic_flag: str) -> ChallengeInstance:
    work_dir = settings.runs_dir / f"{benchmark}_{attempt_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    project = f"xben_{attempt_id}"

    cfg, flag_service = await asyncio.to_thread(
        _build_compose, benchmark, dynamic_flag, work_dir
    )

    base = _compose_base(project, work_dir)
    # up --wait
    rc, out, err = await _run(base + ["up", "-d", "--wait"], timeout=settings.compose_wait_timeout_sec)
    if rc != 0:
        # cleanup partial
        await _run(base + ["down", "-v"], timeout=60)
        raise RuntimeError(f"compose up failed for {benchmark} (rc={rc}):\n{err}\n{out}")

    # embedded: replace original_flag -> dynamic_flag in flag_path
    manifest = get_manifest(benchmark) or {}
    if manifest.get("flag_type") == "embedded" and manifest.get("flag_path") and manifest.get("original_flag"):
        flag_service = flag_service or manifest.get("service")
        if flag_service:
            await _inject_embedded(base, flag_service, manifest["original_flag"], dynamic_flag, manifest["flag_path"])

    port, extra_ports = await _get_host_port(base, cfg, flag_service)
    return ChallengeInstance(project=project, host_port=port, extra_ports=extra_ports, work_dir=work_dir)


async def _inject_embedded(base, service, original, dynamic, flag_path):
    # use | delimiter; flags are FLAG{...} with no | chars
    script = f"sed -i 's|{original}|{dynamic}|g' {shlex.quote(flag_path)}"
    rc, out, err = await _run(
        base + ["exec", "-T", service, "sh", "-c", script], timeout=60
    )
    if rc != 0:
        # non-fatal: flag swap may not apply; solver would fail to match
        raise RuntimeError(f"embedded flag sed failed: {err}")


async def _get_host_port(base, cfg, flag_service) -> tuple[int | None, dict | None]:
    """Return (primary_http_port, extra_ports_dict) for the challenge.

    The proxy must route to a service that has *published* ports to the host.
    The flag_service may be an internal-only service (e.g. XBEN-020's
    internal-service) — in that case we find the user-facing service that
    actually publishes a port.

    extra_ports maps container_port -> host_port for non-primary ports
    (e.g. SSH port 22 for XBEN-042).
    """
    services = cfg.get("services") or {}

    # --- find the gateway service (the one with published ports for proxy) ---
    gateway_svc = flag_service
    gateway_ports = (services.get(gateway_svc) or {}).get("ports") or []
    if not gateway_ports:
        # flag_service has no published ports — scan for the service that does
        for sname, svc in services.items():
            p = svc.get("ports") or []
            if p:
                gateway_svc = sname
                break
        gateway_ports = (services.get(gateway_svc) or {}).get("ports") or []

    # --- determine the primary HTTP container port from published ports ---
    target = None
    all_container_ports = []       # (svc_name, container_port_str)
    for sname, svc in services.items():
        ports = svc.get("ports") or []
        for p in ports:
            if isinstance(p, str) and ":" in p:
                cp = p.split(":")[-1].split("/")[0]
            elif isinstance(p, dict):
                cp = str(p.get("target") or p.get("published") or "")
            elif p:
                cp = str(p).split("/")[0]
            else:
                continue
            all_container_ports.append((sname, cp))

    # pick the primary port: prefer common HTTP ports (80, 5000, 8080, 3000)
    http_like = {"80", "5000", "8080", "3000", "8000", "443"}
    for sname, cp in all_container_ports:
        if sname == gateway_svc and cp in http_like:
            target = cp
            break
    if target is None and all_container_ports:
        # fallback: first published port of gateway service, or first overall
        for sname, cp in all_container_ports:
            if sname == gateway_svc:
                target = cp
                break
        if target is None:
            target = all_container_ports[0][1]

    # --- ask compose for the actual mapped host port (primary) ---
    primary_port = None
    if target and gateway_svc:
        rc, out, err = await _run(base + ["port", gateway_svc, target], timeout=30)
        if rc == 0 and out.strip():
            m = re.search(r":(\d+)\s*$", out.strip())
            if m:
                primary_port = int(m.group(1))

    # --- collect extra (non-primary) port mappings ---
    extra = {}
    for sname, cp in all_container_ports:
        if cp == target and sname == gateway_svc:
            continue        # skip the primary HTTP port
        rc, out, _ = await _run(base + ["port", sname, cp], timeout=30)
        if rc == 0 and out.strip():
            m = re.search(r":(\d+)\s*$", out.strip())
            if m:
                extra[int(cp)] = int(m.group(1))

    return primary_port, extra


async def stop_challenge(project: str, work_dir: Path) -> None:
    base = _compose_base(project, work_dir)
    await _run(base + ["down", "-v"], timeout=120)
    # best-effort dir cleanup
    try:
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception:
        pass
