"""Platform configuration (env-driven)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XBEN_", env_file=".env", extra="ignore")

    # --- runtime paths ---
    # where the xbow validation-benchmarks repo lives (has benchmarks/XBEN-*)
    repo_dir: Path = ROOT / "validation-benchmarks"
    flag_manifest: Path = ROOT / "flag_manifest.json"
    data_dir: Path = ROOT / "data"
    runs_dir: Path = ROOT / "runs"          # per-attempt compose work dirs

    # --- db ---
    database_url: str = ""                  # "" => sqlite+aiosqlite:///<data_dir>/platform.db

    # --- bootstrap admin ---
    admin_user: str = "admin"
    admin_password: str = ""                # if empty, a random one is generated & logged once

    # --- registration / limits ---
    registration_code: str = "ROOTLAB@123"
    max_concurrent_per_user: int = 3

    # --- access ---
    secret_key: str = "dev-secret-change-in-prod"     # session cookie signing
    public_base_url: str = "http://localhost:8000"   # used to build proxied challenge URLs
    allow_direct_port: bool = True                    # also return direct http://host:port URLs
    platform_host: str = "0.0.0.0"
    platform_port: int = 8000

    # --- challenge orchestration ---
    compose_wait_timeout_sec: int = 180
    flag_gomaxprocs: int = 8                          # kills old-Go panic on high-core hosts

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{self.data_dir / 'platform.db'}"

    @property
    def benchmarks_dir(self) -> Path:
        return self.repo_dir / "benchmarks"


settings = Settings()
