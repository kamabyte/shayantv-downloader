"""HLS master playlist parsing and ffmpeg/ffprobe wrappers."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from urllib.parse import urljoin

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


@dataclass(frozen=True)
class Variant:
    url: str
    bandwidth: int
    width: int
    height: int
    audio_group: str | None
    subtitle_group: str | None


@dataclass(frozen=True)
class Rendition:
    type: str  # AUDIO or SUBTITLES
    url: str
    group: str
    language: str | None
    name: str | None


@dataclass(frozen=True)
class Master:
    variants: list[Variant]
    renditions: list[Rendition]

    def best_variant(self) -> Variant:
        if not self.variants:
            raise ValueError("master playlist has no variants")
        return max(self.variants, key=lambda v: (v.width * v.height, v.bandwidth))

    def renditions_for(self, variant: Variant, kind: str) -> list[Rendition]:
        group = variant.audio_group if kind == "AUDIO" else variant.subtitle_group
        return [r for r in self.renditions if r.type == kind and r.group == group]


def _attrs(line: str) -> dict[str, str]:
    body = line.split(":", 1)[1] if ":" in line else ""
    return {k: v.strip('"') for k, v in _ATTR.findall(body)}


def parse_master(text: str, base_url: str) -> Master:
    variants: list[Variant] = []
    renditions: list[Rendition] = []
    pending: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA:"):
            a = _attrs(line)
            if a.get("TYPE") in ("AUDIO", "SUBTITLES") and a.get("URI"):
                renditions.append(
                    Rendition(
                        type=a["TYPE"],
                        url=urljoin(base_url, a["URI"]),
                        group=a.get("GROUP-ID", ""),
                        language=a.get("LANGUAGE"),
                        name=a.get("NAME"),
                    )
                )
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pending = _attrs(line)
        elif not line.startswith("#") and pending is not None:
            width, _, height = pending.get("RESOLUTION", "0x0").partition("x")
            variants.append(
                Variant(
                    url=urljoin(base_url, line),
                    bandwidth=int(pending.get("BANDWIDTH", "0") or 0),
                    width=int(width or 0),
                    height=int(height or 0),
                    audio_group=pending.get("AUDIO"),
                    subtitle_group=pending.get("SUBTITLES"),
                )
            )
            pending = None
    return Master(variants=variants, renditions=renditions)


@cache
def _hls_input_options() -> tuple[str, ...]:
    """The CDN serves fMP4 segments named *.m4v; ffmpeg >= 7.1 rejects that unless told not to be picky."""
    try:
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-h", "demuxer=hls"], capture_output=True, text=True)
    except FileNotFoundError:
        return ()
    return ("-extension_picky", "0") if "extension_picky" in proc.stdout else ()


def build_ffmpeg_command(
    master: Master,
    output: Path,
    *,
    user_agent: str,
    language_map: dict[str, str],
    default_language: str,
    metadata: dict[str, str],
) -> list[str]:
    """Remux the best video variant plus every audio/subtitle rendition into one MKV."""
    variant = master.best_variant()
    audios = master.renditions_for(variant, "AUDIO")
    subtitles = master.renditions_for(variant, "SUBTITLES")

    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    inputs = [variant.url] + [r.url for r in audios] + [r.url for r in subtitles]
    for url in inputs:
        # -rw_timeout (µs) aborts a stalled segment download instead of hanging forever.
        cmd += [*_hls_input_options(), "-rw_timeout", "30000000", "-user_agent", user_agent, "-i", url]

    cmd += ["-map", "0:v:0"]
    if audios:
        for i in range(len(audios)):
            cmd += ["-map", f"{1 + i}:a:0"]
    else:
        cmd += ["-map", "0:a?"]  # audio muxed into the variant itself
    for i in range(len(subtitles)):
        cmd += ["-map", f"{1 + len(audios) + i}:s:0"]

    cmd += ["-c", "copy", "-c:s", "srt"]
    for key, value in metadata.items():
        cmd += ["-metadata", f"{key}={value}"]

    def lang(tag: str | None) -> str:
        return language_map.get((tag or "").lower(), default_language)

    for i, r in enumerate(audios):
        cmd += [f"-metadata:s:a:{i}", f"language={lang(r.language)}"]
    if not audios:
        cmd += ["-metadata:s:a:0", f"language={default_language}"]
    for i, r in enumerate(subtitles):
        cmd += [f"-metadata:s:s:{i}", f"language={lang(r.language)}"]
        if r.name:
            cmd += [f"-metadata:s:s:{i}", f"title={r.name}"]

    cmd += ["-f", "matroska", str(output)]
    return cmd


def run_ffmpeg(cmd: list[str], timeout: float | None = None) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg timed out after {timeout:.0f}s") from None
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-2000:]
        raise RuntimeError(f"ffmpeg exited with {proc.returncode}: {tail or shlex.join(cmd[:8])}")


@dataclass(frozen=True)
class ProbeResult:
    duration: float
    video_streams: int
    audio_streams: int


def probe(path: Path) -> ProbeResult:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    types = [s.get("codec_type") for s in data.get("streams", [])]
    return ProbeResult(
        duration=float(data.get("format", {}).get("duration") or 0),
        video_streams=types.count("video"),
        audio_streams=types.count("audio"),
    )
