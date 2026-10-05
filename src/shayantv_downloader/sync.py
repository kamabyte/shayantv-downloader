"""One sync pass: discover shows and episodes, then download whatever is missing."""

from __future__ import annotations

import logging
import os
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from shayantv_downloader import health, hls, library
from shayantv_downloader.config import Config
from shayantv_downloader.http import fetch, fetch_text
from shayantv_downloader.site import fetch_episodes, fetch_shows
from shayantv_downloader.state import EpisodeRow, State

log = logging.getLogger(__name__)

# Allowed gap between the duration the site reports and what we actually got.
DURATION_TOLERANCE = 0.05
DURATION_SLACK_SECONDS = 10
# A remux copies at many times realtime; anything slower than this is a hung connection.
FFMPEG_TIMEOUT_BASE = 600

# Set on SIGTERM: in-flight episodes finish, queued ones are skipped until the next run.
STOP = threading.Event()


class Cancelled(Exception):
    pass


@dataclass
class SyncResult:
    shows: int = 0
    new_episodes: int = 0
    downloaded: int = 0
    failed: list[str] = field(default_factory=list)
    failed_again: int = 0  # failures of episodes that had already failed in an earlier sync

    def error(self) -> str | None:
        """Why this sync counts as broken for the healthcheck, or None.

        Episodes that keep failing (gone from the CDN, no audio) are left out:
        they would otherwise keep the container unhealthy forever.
        """
        fresh = len(self.failed) - self.failed_again
        if fresh and not self.downloaded:
            return f"all {fresh} new downloads failed"
        return None


def discover(cfg: Config, state: State, only: set[str] | None) -> SyncResult:
    result = SyncResult()
    shows = fetch_shows(cfg)
    if not shows:
        raise RuntimeError(f"no shows found on {cfg.section_url}; has the page layout changed?")
    for show in shows:
        if only and show.slug not in only:
            continue
        result.shows += 1
        folder = state.upsert_show(show, library.sanitize(show.title))
        show_dir = cfg.library_dir / folder
        show_dir.mkdir(parents=True, exist_ok=True)
        _ensure_show_metadata(cfg, show_dir, show.title, show.slug, show.url, show.image_url)
        try:
            episodes = fetch_episodes(cfg, show)
        except Exception as exc:  # one broken show page shouldn't stop the rest
            log.error("%s: could not read episode list: %s", show.slug, exc)
            continue
        added = state.add_episodes(show.slug, episodes)
        result.new_episodes += added
        log.info("%s (%s): %d episodes on site, %d new", show.title, show.slug, len(episodes), added)
    return result


def _ensure_show_metadata(
    cfg: Config, show_dir: Path, title: str, slug: str, url: str, image_url: str | None
) -> None:
    nfo = show_dir / "tvshow.nfo"
    if not nfo.exists():
        library.write_tvshow_nfo(nfo, title=title, slug=slug, url=url)
    if image_url and not (show_dir / "poster.jpg").exists():
        try:
            library.write_show_artwork(show_dir, fetch(image_url, user_agent=cfg.user_agent))
        except Exception as exc:
            log.warning("%s: artwork failed: %s", slug, exc)


def download_episode(cfg: Config, ep: EpisodeRow) -> Path:
    season_dir = library.season_dir(cfg.library_dir / ep.folder, ep.season)
    season_dir.mkdir(parents=True, exist_ok=True)
    base = library.episode_basename(ep.show_title, ep.season, ep.episode, ep.title)
    final = season_dir / f"{base}.mkv"
    partial = season_dir / f"{base}.mkv.part"

    if not final.exists():
        master_text = fetch_text(ep.hls_url, user_agent=cfg.user_agent, timeout=cfg.http_timeout)
        master = hls.parse_master(master_text, ep.hls_url)
        cmd = hls.build_ffmpeg_command(
            master,
            partial,
            user_agent=cfg.user_agent,
            language_map=cfg.language_map,
            default_language=cfg.default_language,
            metadata={"title": ep.title or f"Серия {ep.episode}", "show": ep.show_title},
        )
        try:
            hls.run_ffmpeg(cmd, timeout=FFMPEG_TIMEOUT_BASE + 2 * (ep.duration or 1800))
            _verify(partial, ep.duration)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        os.replace(partial, final)

    library.write_episode_nfo(
        season_dir / f"{base}.nfo",
        show_title=ep.show_title, title=ep.title, season=ep.season, episode=ep.episode,
        uid=ep.uid, duration=ep.duration,
    )
    thumb = season_dir / f"{base}-thumb.jpg"
    if ep.preview_url and not thumb.exists():
        try:
            library.write_thumb(thumb, fetch(ep.preview_url, user_agent=cfg.user_agent))
        except Exception as exc:
            log.warning("%s: thumbnail failed: %s", base, exc)
    return final


def _verify(path: Path, expected: int | None) -> None:
    info = hls.probe(path)
    if info.video_streams < 1 or info.audio_streams < 1:
        raise RuntimeError(f"incomplete file: {info.video_streams} video / {info.audio_streams} audio streams")
    if expected:
        allowed = max(DURATION_SLACK_SECONDS, expected * DURATION_TOLERANCE)
        if abs(info.duration - expected) > allowed:
            raise RuntimeError(f"duration {info.duration:.0f}s, expected ~{expected}s")


def _download_with_retries(cfg: Config, ep: EpisodeRow) -> Path:
    for attempt in range(1, cfg.download_attempts + 1):
        if STOP.is_set():
            raise Cancelled
        try:
            return download_episode(cfg, ep)
        except Exception as exc:
            if attempt == cfg.download_attempts:
                raise
            delay = 15 * attempt
            log.warning("S%02dE%02d %s: attempt %d failed (%s); retrying in %ds",
                        ep.season, ep.episode, ep.show_title, attempt, exc, delay)
            STOP.wait(delay)
    raise AssertionError("unreachable")


def download_pending(cfg: Config, state: State, only: set[str] | None, limit: int | None, result: SyncResult) -> None:
    todo = state.todo(only)
    if limit is not None:
        todo = todo[:limit]
    if not todo:
        log.info("Nothing to download")
        return
    log.info("Downloading %d episode(s) with concurrency %d", len(todo), cfg.concurrency)
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        futures = {pool.submit(_download_with_retries, cfg, ep): ep for ep in todo}
        for i, future in enumerate(as_completed(futures), 1):
            ep = futures[future]
            label = f"{ep.show_title} S{ep.season:02d}E{ep.episode:02d}"
            try:
                path = future.result()
            except Cancelled:
                continue
            except Exception as exc:
                state.mark_failed(ep.uid, str(exc))
                result.failed.append(label)
                result.failed_again += ep.attempts > 0
                log.error("[%d/%d] FAILED %s: %s", i, len(todo), label, exc)
            else:
                health.beat(cfg)
                state.mark_done(ep.uid, path)
                result.downloaded += 1
                log.info("[%d/%d] done %s", i, len(todo), label)


def refresh_jellyfin(cfg: Config) -> None:
    if not (cfg.jellyfin_url and cfg.jellyfin_api_key):
        return
    url = cfg.jellyfin_url.rstrip("/") + "/Library/Refresh"
    request = urllib.request.Request(
        url, method="POST", data=b"",
        headers={"Authorization": f'MediaBrowser Token="{cfg.jellyfin_api_key}"'},
    )
    try:
        with urllib.request.urlopen(request, timeout=30):
            pass
        log.info("Asked Jellyfin to rescan libraries")
    except Exception as exc:
        log.warning("Jellyfin refresh failed: %s", exc)


def run_sync(cfg: Config, *, only: set[str] | None = None, limit: int | None = None, dry_run: bool = False) -> SyncResult:
    cfg.library_dir.mkdir(parents=True, exist_ok=True)
    state = State(cfg.db_path)
    try:
        result = discover(cfg, state, only)
        if dry_run:
            todo = state.todo(only)
            for ep in todo[:limit] if limit is not None else todo:
                log.info("would download: %s/Season %02d/%s", ep.folder, ep.season,
                         library.episode_basename(ep.show_title, ep.season, ep.episode, ep.title))
            return result
        download_pending(cfg, state, only, limit, result)
        if result.downloaded:
            refresh_jellyfin(cfg)
        return result
    finally:
        state.close()
