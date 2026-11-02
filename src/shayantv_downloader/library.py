"""Jellyfin library layout: folder/file naming, NFO metadata and artwork."""

from __future__ import annotations

import io
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

POSTER_SIZE = (1000, 1500)
STUDIO = "Шаян ТВ"
# Bump when the artwork recipe changes: existing shows get redrawn on the next sync.
ARTWORK_VERSION = "2"
ARTWORK_STAMP = ".artwork-version"
# How much a crop at the very edge of the image is penalised against one in the centre.
CROP_CENTRE_BIAS = 0.25

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


def _busiest_crop_x(img: Image.Image, crop_width: int) -> int:
    """Left edge of the crop window with the most detail: characters rather than sky."""
    height = 128
    scale = height / img.height
    edges = img.convert("L").resize((round(img.width * scale), height)).filter(ImageFilter.FIND_EDGES)
    edges = edges.crop((1, 1, edges.width - 1, edges.height - 1))  # FIND_EDGES leaves the border unfiltered
    columns = edges.resize((edges.width, 1), Image.Resampling.BOX).tobytes()
    window = round(crop_width * scale)
    last = len(columns) - window
    if last <= 0:
        return (img.width - crop_width) // 2

    def score(x: int) -> float:
        off_centre = abs(x - last / 2) / (last / 2)
        # +1 so that art with no detail at all still falls back to the centre.
        return (sum(columns[x : x + window]) + 1) * (1 - CROP_CENTRE_BIAS * off_centre)

    return min(round(max(range(last + 1), key=score) / scale), img.width - crop_width)


def make_poster(art: Image.Image) -> Image.Image:
    """Full-bleed 2:3 poster cut from the site's landscape art, because players show posters as tall cards."""
    width, height = POSTER_SIZE
    crop_width = round(art.height * width / height)
    if crop_width >= art.width:  # already portrait or square: only the height can be trimmed
        return ImageOps.fit(art, POSTER_SIZE)
    x = _busiest_crop_x(art, crop_width)
    return art.crop((x, 0, x + crop_width, art.height)).resize(POSTER_SIZE, Image.Resampling.LANCZOS)


def artwork_is_current(show_dir: Path) -> bool:
    try:
        return (show_dir / ARTWORK_STAMP).read_text().strip() == ARTWORK_VERSION
    except FileNotFoundError:
        return False


def write_show_artwork(show_dir: Path, image_data: bytes) -> None:
    img = _open(image_data)
    _save_jpeg(img, show_dir / "fanart.jpg")
    _save_jpeg(img, show_dir / "landscape.jpg")
    _save_jpeg(make_poster(img), show_dir / "poster.jpg")
    (show_dir / ARTWORK_STAMP).write_text(ARTWORK_VERSION)


def write_thumb(path: Path, image_data: bytes) -> None:
    _save_jpeg(_open(image_data), path)
