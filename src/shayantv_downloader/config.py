"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


def _parse_language_map(raw: str) -> dict[str, str]:
    """Parse "en=tat,ru=rus" into {"en": "tat", "ru": "rus"}."""
    result: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            src, dst = pair.split("=", 1)
            result[src.strip().lower()] = dst.strip()
    return result


@dataclass(frozen=True)
class Config:
    library_dir: Path
    state_dir: Path
    base_url: str = "https://shayantv.ru"
    section: str = "/cartoons/"
    concurrency: int = 2
    # The CDN labels the Tatar dub as "en"; remap HLS language tags to ISO 639-2.
    language_map: dict[str, str] = field(default_factory=lambda: {"en": "tat", "ru": "rus"})
    default_language: str = "tat"
    interval_hours: float = 12.0
    download_attempts: int = 3
    http_timeout: float = 30.0
    user_agent: str = DEFAULT_USER_AGENT
    jellyfin_url: str | None = None
    jellyfin_api_key: str | None = None

    @property
    def section_url(self) -> str:
        return self.base_url.rstrip("/") + self.section

    @property
    def db_path(self) -> Path:
        return self.state_dir / "state.sqlite3"

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ
        kwargs: dict = {
            "library_dir": Path(env.get("LIBRARY_DIR", "./library")),
            "state_dir": Path(env.get("STATE_DIR", "./data")),
        }
        if "BASE_URL" in env:
            kwargs["base_url"] = env["BASE_URL"]
        if "CONCURRENCY" in env:
            kwargs["concurrency"] = max(1, int(env["CONCURRENCY"]))
        if "LANGUAGE_MAP" in env:
            kwargs["language_map"] = _parse_language_map(env["LANGUAGE_MAP"])
        if "DEFAULT_LANGUAGE" in env:
            kwargs["default_language"] = env["DEFAULT_LANGUAGE"]
        if "INTERVAL_HOURS" in env:
            kwargs["interval_hours"] = float(env["INTERVAL_HOURS"])
        if "DOWNLOAD_ATTEMPTS" in env:
            kwargs["download_attempts"] = max(1, int(env["DOWNLOAD_ATTEMPTS"]))
        kwargs["jellyfin_url"] = env.get("JELLYFIN_URL") or None
        kwargs["jellyfin_api_key"] = env.get("JELLYFIN_API_KEY") or None
        return cls(**kwargs)
