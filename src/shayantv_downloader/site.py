"""Scraping shayantv.ru: the show list and each show's embedded episode playlist."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

from shayantv_downloader.config import Config
from shayantv_downloader.http import fetch_text


@dataclass(frozen=True)
class Show:
    slug: str
    title: str
    url: str
    image_url: str | None


@dataclass(frozen=True)
class Episode:
    uid: str  # stable ID derived from the HLS URL; the site's episode numbers are unreliable
    season: int
    site_episode: int
    title: str
    hls_url: str
    preview_url: str | None
    duration: int | None  # seconds


class _ShowListParser(HTMLParser):
    """Collects <a class="l-card-inner"> cards with their <img> and <h4> title."""

    def __init__(self) -> None:
        super().__init__()
        self.cards: list[dict] = []
        self._card: dict | None = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and "l-card-inner" in (a.get("class") or "").split():
            self._card = {"href": a.get("href"), "img": None, "alt": None, "title": ""}
        elif self._card is not None and tag == "img":
            self._card["img"] = a.get("src")
            self._card["alt"] = a.get("alt")
        elif self._card is not None and tag == "h4":
            self._in_title = True

    def handle_data(self, data):
        if self._card is not None and self._in_title:
            self._card["title"] += data

    def handle_endtag(self, tag):
        if tag == "h4":
            self._in_title = False
        elif tag == "a" and self._card is not None:
            self.cards.append(self._card)
            self._card = None


def parse_show_list(html: str, page_url: str, section: str) -> list[Show]:
    parser = _ShowListParser()
    parser.feed(html)
    shows: list[Show] = []
    seen: set[str] = set()
    for card in parser.cards:
        href = card["href"] or ""
        url = urljoin(page_url, href)
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        slug = path.removeprefix(section.strip("/")).strip("/")
        if not slug or "/" in slug or slug in seen:
            continue
        seen.add(slug)
        title = (card["title"] or card["alt"] or slug).strip()
        image = urljoin(page_url, card["img"]) if card["img"] else None
        shows.append(Show(slug=slug, title=title, url=url, image_url=image))
    return shows


_APP_DATA = re.compile(r"\bJSAppData\s*=\s*")
_HLS_HASH = re.compile(r"/hls/([0-9a-f]{16,})/")


def extract_app_data(html: str) -> dict:
    match = _APP_DATA.search(html)
    if not match:
        raise ValueError("JSAppData not found on page")
    data, _ = json.JSONDecoder().raw_decode(html, match.end())
    return data


def episode_uid(hls_url: str) -> str:
    match = _HLS_HASH.search(hls_url)
    if match:
        return match.group(1)
    return hashlib.sha1(hls_url.encode()).hexdigest()


def parse_episodes(app_data: dict, page_url: str) -> list[Episode]:
    """Return episodes in page order, skipping ones without a playable stream."""
    episodes: list[Episode] = []
    for season_block in app_data.get("VideoPlaylist") or []:
        for raw in season_block.get("episodes") or []:
            hls = raw.get("hls")
            if not hls or raw.get("noPublicAccess") or raw.get("CDNReady") is False:
                continue
            season = 1 if app_data.get("noSeason") else _to_int(raw.get("season") or season_block.get("season"), 1)
            preview = raw.get("preview")
            episodes.append(
                Episode(
                    uid=episode_uid(hls),
                    season=season,
                    site_episode=_to_int(raw.get("episode"), 0),
                    title=" ".join(str(raw.get("title") or "").split()),
                    hls_url=hls,
                    preview_url=urljoin(page_url, preview) if preview else None,
                    duration=_to_int(raw.get("duration"), 0) or None,
                )
            )
    return episodes


def _to_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def fetch_shows(cfg: Config) -> list[Show]:
    html = fetch_text(cfg.section_url, user_agent=cfg.user_agent, timeout=cfg.http_timeout)
    return parse_show_list(html, cfg.section_url, cfg.section)


def fetch_episodes(cfg: Config, show: Show) -> list[Episode]:
    html = fetch_text(show.url, user_agent=cfg.user_agent, timeout=cfg.http_timeout)
    return parse_episodes(extract_app_data(html), show.url)
