"""Backend configuration (environment / .env)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_dir: Path = Path("data")
    # empty = a SQLite file under data_dir; production sets a PostgreSQL URL (e.g. Neon)
    database_url: str | None = None
    # the deployed commit, shown by /api/health so CD can confirm what is live (Render sets RENDER_GIT_COMMIT)
    git_commit: str | None = Field(None, validation_alias=AliasChoices("GIT_COMMIT", "RENDER_GIT_COMMIT"))
    frontend_dist: Path = Path("frontend/dist")
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    telemetry_webhook_secret: str = "dev-telemetry-secret-change-me"
    webhook_tolerance_s: int = 300
    enable_telemetry_simulator: bool = True
    default_speed_ms: int = 450
    log_level: str = "INFO"

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'negotiator.db').as_posix()}"

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings()
