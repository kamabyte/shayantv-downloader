"""Jellyfin library layout: folder/file naming, NFO metadata and artwork."""

from __future__ import annotations

import io
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

POSTER_SIZE = (1000, 1500)
STUDIO = "Шаян ТВ"

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str, max_len: int = 120) -> str:
    """Make a name safe for SMB/NFS/ext4/btrfs and Windows clients."""
    cleaned = _FORBIDDEN.sub(" ", name)
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned[:max_len].rstrip(" .") or "untitled"


def season_dir(show_dir: Path, season: int) -> Path:
    return show_dir / f"Season {season:02d}"


def episode_basename(show_title: str, season: int, episode: int, title: str) -> str:
    base = f"{sanitize(show_title)} - S{season:02d}E{episode:02d}"
    return f"{base} - {sanitize(title)}" if title else base


def _write_xml(root: ET.Element, path: Path) -> None:
    ET.indent(root)
    tmp = path.with_name(path.name + ".tmp")
    ET.ElementTree(root).write(tmp, encoding="utf-8", xml_declaration=True)
    os.replace(tmp, path)


def _sub(parent: ET.Element, tag: str, text: str | int | None, **attrs) -> None:
    if text is None or text == "":
        return
    el = ET.SubElement(parent, tag, attrs)
    el.text = str(text)


def write_tvshow_nfo(path: Path, *, title: str, slug: str, url: str) -> None:
    root = ET.Element("tvshow")
    _sub(root, "title", title)
    _sub(root, "originaltitle", title)
    _sub(root, "sorttitle", title)
    _sub(root, "plot", None)
    _sub(root, "studio", STUDIO)
    for genre in ("Мультфильм", "Детский"):
        _sub(root, "genre", genre)
    _sub(root, "uniqueid", slug, type="shayantv", default="true")
    _sub(root, "website", url)
    # Stops Jellyfin from overwriting our metadata with online lookups.
    _sub(root, "lockdata", "true")
    _write_xml(root, path)


def write_episode_nfo(
    path: Path, *, show_title: str, title: str, season: int, episode: int, uid: str, duration: int | None
) -> None:
    root = ET.Element("episodedetails")
    _sub(root, "title", title or f"Серия {episode}")
    _sub(root, "showtitle", show_title)
    _sub(root, "season", season)
    _sub(root, "episode", episode)
    if duration:
        _sub(root, "runtime", max(1, round(duration / 60)))
    _sub(root, "studio", STUDIO)
    _sub(root, "uniqueid", uid, type="shayantv", default="true")
    _sub(root, "lockdata", "true")
    _write_xml(root, path)


def _save_jpeg(img: Image.Image, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    img.convert("RGB").save(tmp, "JPEG", quality=90, optimize=True)
    os.replace(tmp, path)


def _open(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img.convert("RGB")


def make_poster(landscape: Image.Image) -> Image.Image:
    """Portrait 2:3 poster: the full landscape art over a blurred, darkened copy of itself."""
    background = ImageOps.fit(landscape, POSTER_SIZE).filter(ImageFilter.GaussianBlur(40))
    background = ImageEnhance.Brightness(background).enhance(0.55)
    foreground = ImageOps.contain(landscape, POSTER_SIZE)
    x = (POSTER_SIZE[0] - foreground.width) // 2
    y = (POSTER_SIZE[1] - foreground.height) // 2
    background.paste(foreground, (x, y))
    return background


def write_show_artwork(show_dir: Path, image_data: bytes) -> None:
    img = _open(image_data)
    _save_jpeg(img, show_dir / "fanart.jpg")
    _save_jpeg(img, show_dir / "landscape.jpg")
    _save_jpeg(make_poster(img), show_dir / "poster.jpg")


def write_thumb(path: Path, image_data: bytes) -> None:
    _save_jpeg(_open(image_data), path)
