"""Liveness file for the container healthcheck (`shayantv-dl health`).

Unhealthy when the daemon stops making progress (no heartbeat) or the last sync
broke as a whole: the site couldn't be parsed, or every first-time download failed.
Single episode failures are normal and retried, so they don't count.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from shayantv_downloader.config import Config

STALE_AFTER = timedelta(hours=2)


def _path(cfg: Config):
    return cfg.state_dir / "health.json"


def _load(cfg: Config) -> dict:
    try:
        return json.loads(_path(cfg).read_text())
    except (OSError, ValueError):
        return {}


def _save(cfg: Config, data: dict) -> None:
    path = _path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def beat(cfg: Config) -> None:
    data = _load(cfg)
    data["heartbeat"] = datetime.now(timezone.utc).isoformat()
    _save(cfg, data)


def sync_finished(cfg: Config, error: str | None) -> None:
    data = _load(cfg)
    data["heartbeat"] = datetime.now(timezone.utc).isoformat()
    data["last_error"] = error
    _save(cfg, data)


def check(cfg: Config) -> tuple[bool, str]:
    data = _load(cfg)
    if "heartbeat" not in data:
        return False, "no heartbeat yet"
    age = datetime.now(timezone.utc) - datetime.fromisoformat(data["heartbeat"])
    if age > STALE_AFTER:
        return False, f"no progress for {age}"
    if data.get("last_error"):
        return False, f"last sync failed: {data['last_error']}"
    return True, "ok"
