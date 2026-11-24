"""Runtime configuration, read from environment variables (and a .env file if present)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip() else default


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "data")).resolve())
    database_url: str = ""
    secret_key: str = ""

    # Where listings come from: "demo", "serpapi" or "facebook".
    source: str = field(default_factory=lambda: os.getenv("SOURCE", "demo").strip().lower())
    serpapi_key: str = field(default_factory=lambda: os.getenv("SERPAPI_KEY", ""))
    fb_storage_state: Path | None = None
    fb_headless: bool = field(default_factory=lambda: _bool("FB_HEADLESS", True))

    # Daily job.
    scheduler_enabled: bool = field(default_factory=lambda: _bool("SCHEDULER_ENABLED", True))
    schedule_hour: int = field(default_factory=lambda: _int("SCHEDULE_HOUR", 6))
    schedule_minute: int = field(default_factory=lambda: _int("SCHEDULE_MINUTE", 17))
    schedule_tz: str = field(default_factory=lambda: os.getenv("SCHEDULE_TZ", "UTC"))
    max_requests_per_run: int = field(default_factory=lambda: _int("MAX_REQUESTS_PER_RUN", 150))
    # Also search the buyer's own area (off by default: they can browse their own Marketplace).
    include_own_area: bool = field(default_factory=lambda: _bool("INCLUDE_OWN_AREA", False))

    # Seed value for the invite code; admins can rotate it in the app.
    invite_code: str = field(default_factory=lambda: os.getenv("INVITE_CODE", ""))
    # Mark the session cookie Secure (turn on when served over HTTPS).
    secure_cookies: bool = field(default_factory=lambda: _bool("SECURE_COOKIES", False))

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            self.database_url = os.getenv("DATABASE_URL") or f"sqlite:///{self.data_dir / 'app.db'}"
        if not self.secret_key:
            self.secret_key = os.getenv("SECRET_KEY") or self._persistent_secret()
        if self.fb_storage_state is None:
            raw = os.getenv("FB_STORAGE_STATE")
            self.fb_storage_state = Path(raw).resolve() if raw else self.data_dir / "fb_state.json"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    def _persistent_secret(self) -> str:
        """Generate a secret once and keep it in the data dir so sessions survive restarts."""
        path = self.data_dir / ".secret_key"
        if path.exists():
            return path.read_text().strip()
        value = secrets.token_urlsafe(48)
        path.write_text(value)
        path.chmod(0o600)
        return value


settings = Settings()
