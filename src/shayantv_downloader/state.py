"""SQLite state: which episodes exist, their permanent numbers, and download status."""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from shayantv_downloader.site import Episode, Show

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
    slug        TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    folder      TEXT NOT NULL,
    image_url   TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
    uid           TEXT PRIMARY KEY,
    show_slug     TEXT NOT NULL REFERENCES shows(slug),
    season        INTEGER NOT NULL,
    episode       INTEGER NOT NULL,
    site_episode  INTEGER NOT NULL,
    title         TEXT NOT NULL,
    hls_url       TEXT NOT NULL,
    preview_url   TEXT,
    duration      INTEGER,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    path          TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    downloaded_at TEXT,
    UNIQUE (show_slug, season, episode)
);
"""

_TITLE_NUMBER = re.compile(r"^\s*(\d+)\s*(?:серия|выпуск)", re.IGNORECASE)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def assign_numbers(new: list[Episode], taken: set[int]) -> dict[str, int]:
    """Give each new episode of one season a permanent episode number.

    The site's own numbers contain duplicates (e.g. two "episode 1" entries where the
    title says "2 серия"), so prefer the number in the title, then the site number.
    Episodes whose preferred numbers are all taken are appended after the highest
    number, in a second pass, so they never push later episodes out of place.
    """
    taken = set(taken)
    result: dict[str, int] = {}
    leftovers: list[Episode] = []
    for ep in new:
        match = _TITLE_NUMBER.match(ep.title)
        candidates = [int(match.group(1))] if match else []
        candidates.append(ep.site_episode)
        chosen = next((n for n in candidates if n > 0 and n not in taken), None)
        if chosen is None:
            leftovers.append(ep)
        else:
            taken.add(chosen)
            result[ep.uid] = chosen
    for ep in leftovers:
        chosen = max(taken, default=0) + 1
        taken.add(chosen)
        result[ep.uid] = chosen
    return result


@dataclass(frozen=True)
class EpisodeRow:
    uid: str
    show_slug: str
    show_title: str
    folder: str
    season: int
    episode: int
    title: str
    hls_url: str
    preview_url: str | None
    duration: int | None
    attempts: int


class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    def upsert_show(self, show: Show, folder: str) -> str:
        """Record a show and return its folder name (fixed on first sight)."""
        with self._lock:
            row = self._conn.execute("SELECT folder FROM shows WHERE slug = ?", (show.slug,)).fetchone()
            ts = now()
            if row:
                self._conn.execute(
                    "UPDATE shows SET title = ?, image_url = ?, last_seen = ? WHERE slug = ?",
                    (show.title, show.image_url, ts, show.slug),
                )
                return row["folder"]
            self._conn.execute(
                "INSERT INTO shows (slug, title, folder, image_url, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                (show.slug, show.title, folder, show.image_url, ts, ts),
            )
            return folder

    def add_episodes(self, show_slug: str, episodes: list[Episode]) -> int:
        """Insert episodes not seen before; refresh URLs of known ones. Returns count of new."""
        with self._lock:
            ts = now()
            known = {
                r["uid"]
                for r in self._conn.execute("SELECT uid FROM episodes WHERE show_slug = ?", (show_slug,))
            }
            fresh: dict[int, list[Episode]] = {}
            seen_now: set[str] = set()
            for ep in episodes:
                if ep.uid in seen_now:
                    continue  # the site sometimes lists the same stream twice
                seen_now.add(ep.uid)
                if ep.uid in known:
                    self._conn.execute(
                        "UPDATE episodes SET hls_url = ?, preview_url = ?, last_seen = ? WHERE uid = ?",
                        (ep.hls_url, ep.preview_url, ts, ep.uid),
                    )
                else:
                    fresh.setdefault(ep.season, []).append(ep)

            added = 0
            for season, eps in fresh.items():
                taken = {
                    r["episode"]
                    for r in self._conn.execute(
                        "SELECT episode FROM episodes WHERE show_slug = ? AND season = ?", (show_slug, season)
                    )
                }
                numbers = assign_numbers(eps, taken)
                for ep in eps:
                    self._conn.execute(
                        """INSERT INTO episodes (uid, show_slug, season, episode, site_episode, title, hls_url,
                                                 preview_url, duration, first_seen, last_seen)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (ep.uid, show_slug, season, numbers[ep.uid], ep.site_episode, ep.title, ep.hls_url,
                         ep.preview_url, ep.duration, ts, ts),
                    )
                    added += 1
            return added

    def todo(self, only: set[str] | None = None) -> list[EpisodeRow]:
        query = """SELECT e.*, s.title AS show_title, s.folder FROM episodes e JOIN shows s ON s.slug = e.show_slug
                   WHERE e.status != 'done' ORDER BY s.title, e.season, e.episode"""
        with self._lock:
            rows = self._conn.execute(query).fetchall()
        return [
            EpisodeRow(
                uid=r["uid"], show_slug=r["show_slug"], show_title=r["show_title"], folder=r["folder"],
                season=r["season"], episode=r["episode"], title=r["title"], hls_url=r["hls_url"],
                preview_url=r["preview_url"], duration=r["duration"], attempts=r["attempts"],
            )
            for r in rows
            if not only or r["show_slug"] in only
        ]

    def mark_done(self, uid: str, path: Path) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE episodes SET status = 'done', path = ?, downloaded_at = ?, last_error = NULL WHERE uid = ?",
                (str(path), now(), uid),
            )

    def mark_failed(self, uid: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE episodes SET status = 'failed', attempts = attempts + 1, last_error = ? WHERE uid = ?",
                (error[-1000:], uid),
            )

    def summary(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """SELECT s.title, s.slug,
                          SUM(e.status = 'done') AS done,
                          SUM(e.status = 'pending') AS pending,
                          SUM(e.status = 'failed') AS failed
                   FROM shows s LEFT JOIN episodes e ON e.show_slug = s.slug
                   GROUP BY s.slug ORDER BY s.title"""
            ).fetchall()

    def failures(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT show_slug, season, episode, attempts, last_error FROM episodes WHERE status = 'failed'"
            ).fetchall()
