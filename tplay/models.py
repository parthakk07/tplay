"""Tracks, stream descriptions, and small display helpers."""

from __future__ import annotations

from dataclasses import dataclass


ART_COLORS = (
    "#1db954",
    "#1e90ff",
    "#e13300",
    "#af2896",
    "#509bf5",
    "#f59b23",
    "#148a8a",
    "#e8115b",
    "#477d95",
    "#8c67ab",
    "#2d46b9",
    "#ba5d07",
)


@dataclass(frozen=True)
class Track:
    """One playable song. video_id is empty when it still needs a YouTube search."""

    video_id: str
    title: str
    artist: str
    duration: int | None
    query: str = ""

    @property
    def watch_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def duration_text(self) -> str:
        return format_duration(self.duration)


@dataclass(frozen=True)
class Stream:
    """A direct media URL plus the headers the host expects."""

    url: str
    headers: dict[str, str]


def format_duration(seconds: int | float | None) -> str:
    """Format a length as m:ss or h:mm:ss. Unknown lengths become --:--."""

    if seconds is None:
        return "--:--"
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "--:--"
    if total < 0:
        return "--:--"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def art_color(key: str) -> str:
    """Pick a stable cover color for a track id."""

    total = sum(key.encode("utf-8", "replace"))
    return ART_COLORS[total % len(ART_COLORS)]


def clean_artist(name: str) -> str:
    """Drop the YouTube Music 'Topic' suffix from auto-generated channels."""

    cleaned = " ".join(name.split())
    suffix = " - Topic"
    if cleaned.endswith(suffix):
        cleaned = cleaned[: -len(suffix)].strip()
    return cleaned or "Unknown artist"


def one_line(value: object, fallback: str) -> str:
    """Collapse whitespace into a single display line."""

    text = " ".join(str(value or "").split())
    return text or fallback


def short_error(exc: BaseException) -> str:
    """One-line error suitable for the status row."""

    text = " ".join(str(exc).split()) or exc.__class__.__name__
    if len(text) > 180:
        return text[:179] + "…"
    return text
