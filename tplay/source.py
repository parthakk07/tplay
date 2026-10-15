"""Search YouTube and resolve a direct audio URL. Nothing is written to disk."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import yt_dlp

from tplay.models import Stream, Track, clean_artist, one_line

SEARCH_LIMIT = 15
PLAYLIST_LIMIT = 100
# The public Spotify embed page lists at most this many songs.
SPOTIFY_PAGE_LIMIT = 50

_YOUTUBE_HOSTS = {"youtube.com", "youtu.be", "music.youtube.com", "m.youtube.com"}
_SPOTIFY_HOSTS = {"open.spotify.com", "spotify.com", "play.spotify.com"}
_SPOTIFY_LINK_HOSTS = {"spotify.link", "spotify.app.link"}
_PLAYLIST_ID = re.compile(r"(?:PL|UU|OLAK5uy_)[A-Za-z0-9_-]{8,}")
_SPOTIFY_URI = re.compile(r"spotify:(playlist|album|track):([A-Za-z0-9]+)")
_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)
_BROWSER = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class SourceError(Exception):
    """Search or stream resolution failed."""


class _QuietLogger:
    """Swallow yt-dlp's own log lines. The UI reports errors itself."""

    def debug(self, msg: str) -> None:
        return None

    def info(self, msg: str) -> None:
        return None

    def warning(self, msg: str) -> None:
        return None

    def error(self, msg: str) -> None:
        return None


def _base_options() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _QuietLogger(),
        "socket_timeout": 15,
        "retries": 2,
        "extractor_retries": 2,
        "skip_download": True,
    }


def _duration(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return seconds


def track_from_entry(entry: dict[str, Any]) -> Track | None:
    """Build a track from one yt-dlp result, or None if it cannot be played."""

    video_id = str(entry.get("id") or "").strip()
    if not video_id or video_id.startswith("MPRE"):
        return None
    title = one_line(entry.get("title"), "")
    if not title:
        return None
    artist = clean_artist(
        one_line(entry.get("channel") or entry.get("uploader") or entry.get("artist"), "")
    )
    return Track(
        video_id=video_id,
        title=title,
        artist=artist,
        duration=_duration(entry.get("duration")),
    )


def tracks_from_info(info: dict[str, Any] | None, limit: int = SEARCH_LIMIT) -> list[Track]:
    """Flatten a yt-dlp info dict into a short list of tracks."""

    if not info:
        return []
    entries = info.get("entries")
    if entries is None:
        entries = [info]
    tracks: list[Track] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        track = track_from_entry(entry)
        if track is None:
            continue
        tracks.append(track)
        if len(tracks) >= limit:
            break
    return tracks


def spotify_target(text: str) -> tuple[str, str] | None:
    """Return (playlist|album|track|link, id or url) for a Spotify paste."""

    raw = " ".join(text.split())
    uri = _SPOTIFY_URI.fullmatch(raw)
    if uri:
        return uri.group(1), uri.group(2)
    candidate = raw
    if "://" not in candidate and (
        candidate.startswith("open.spotify.com/")
        or candidate.startswith("spotify.com/")
        or candidate.startswith("www.spotify.com/")
    ):
        candidate = "https://" + candidate
    if "://" not in candidate:
        return None
    parsed = urlparse(candidate)
    host = parsed.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host in _SPOTIFY_LINK_HOSTS:
        return "link", candidate
    if host not in _SPOTIFY_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0].startswith("intl-"):
        parts = parts[1:]
    if parts and parts[0] == "embed":
        parts = parts[1:]
    if len(parts) >= 2 and parts[0] in {"playlist", "album", "track"} and parts[1]:
        return parts[0], parts[1]
    return None


def classify_query(text: str) -> str:
    """Tell a normal search from a YouTube or Spotify link."""

    raw = " ".join(text.split())
    if not raw:
        return "search"
    spotify = spotify_target(raw)
    if spotify is not None:
        return "video" if spotify[0] == "track" else "playlist"
    if _PLAYLIST_ID.fullmatch(raw):
        return "playlist"
    candidate = raw
    if "://" not in candidate and (
        candidate.startswith("www.")
        or candidate.startswith("youtube.com/")
        or candidate.startswith("youtu.be/")
        or candidate.startswith("music.youtube.com/")
        or candidate.startswith("m.youtube.com/")
    ):
        candidate = "https://" + candidate
    if "://" not in candidate:
        return "search"
    parsed = urlparse(candidate)
    host = parsed.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host not in _YOUTUBE_HOSTS:
        return "search"
    query = parse_qs(parsed.query)
    playlist_id = (query.get("list") or [""])[0].strip()
    if playlist_id:
        return "playlist"
    path = parsed.path.strip("/")
    if host == "youtu.be" and path:
        return "video"
    if path.startswith(("watch", "shorts/", "embed/", "live/")):
        return "video"
    if path == "playlist":
        return "playlist"
    return "search"


def tracks_from_spotify_html(html: str, limit: int = SPOTIFY_PAGE_LIMIT) -> tuple[str, list[Track], int | None]:
    """Read the song list from a public Spotify embed page."""

    match = _NEXT_DATA.search(html)
    if not match:
        raise SourceError("Could not read that Spotify link")
    try:
        payload = json.loads(match.group(1))
        page = payload["props"]["pageProps"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SourceError("Could not read that Spotify link") from exc
    state = page.get("state") if isinstance(page, dict) else None
    entity = (state or {}).get("data", {}).get("entity") if isinstance(state, dict) else None
    if not isinstance(entity, dict):
        message = page.get("title") if isinstance(page, dict) else ""
        raise SourceError(one_line(message, "Could not open that Spotify link"))
    title = one_line(entity.get("title") or entity.get("name"), "Playlist")
    rows = entity.get("trackList")
    if not isinstance(rows, list) or not rows:
        rows = [entity]
    tracks: list[Track] = []
    for item in rows:
        track = _track_from_spotify_row(item)
        if track is None:
            continue
        tracks.append(track)
        if len(tracks) >= limit:
            break
    if not tracks:
        raise SourceError("That Spotify link has no playable songs")
    return title, tracks, None


def _track_from_spotify_row(item: object) -> Track | None:
    if not isinstance(item, dict):
        return None
    title = one_line(item.get("title") or item.get("name"), "")
    if not title:
        return None
    artist = one_line(item.get("subtitle"), "")
    if not artist:
        names: list[str] = []
        artists = item.get("artists")
        if isinstance(artists, list):
            for entry in artists:
                if isinstance(entry, dict):
                    name = one_line(entry.get("name"), "")
                    if name:
                        names.append(name)
        artist = ", ".join(names)
    artist = artist or "Unknown artist"
    query = title if artist == "Unknown artist" else f"{artist} - {title}"
    return Track(
        video_id="",
        title=title,
        artist=artist,
        duration=_milliseconds(item.get("duration")),
        query=query,
    )


def _milliseconds(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = int(value / 1000)
    if seconds <= 0:
        return None
    return seconds


def _http_get(url: str) -> tuple[str, str]:
    """Fetch a page. Returns the final URL and the body. Nothing is saved."""

    request = Request(url, headers={"User-Agent": _BROWSER, "Accept": "text/html,application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", "replace")
            return response.geturl(), body
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise SourceError("Could not open that Spotify link") from exc


def normalize_link(text: str) -> str:
    """Turn a pasted id or bare host path into a URL yt-dlp can open."""

    raw = " ".join(text.split())
    if _PLAYLIST_ID.fullmatch(raw):
        return f"https://www.youtube.com/playlist?list={raw}"
    if "://" not in raw:
        return "https://" + raw
    return raw


class YouTubeSource:
    """YouTube search and audio-stream resolution via yt-dlp. download stays off."""

    def search(self, query: str, limit: int = SEARCH_LIMIT) -> list[Track]:
        query = " ".join(query.split())
        if not query:
            return []
        limit = max(1, min(25, limit))
        options = _base_options()
        options.update(
            {
                "extract_flat": "in_playlist",
                "ignoreerrors": True,
                "noplaylist": True,
            }
        )
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        except Exception as exc:
            raise SourceError(str(exc) or "Search failed") from exc
        return tracks_from_info(info, limit)

    def collect(self, url: str, kind: str, limit: int = PLAYLIST_LIMIT) -> tuple[str, list[Track], int | None]:
        """Read a video or playlist link. Returns title, tracks, and the full count."""

        if spotify_target(url) is not None:
            return self._collect_spotify(url, limit)
        limit = max(1, min(PLAYLIST_LIMIT, limit))
        options = _base_options()
        options.update(
            {
                "extract_flat": "in_playlist",
                "ignoreerrors": True,
                "noplaylist": kind != "playlist",
                "playlistend": limit,
            }
        )
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(normalize_link(url), download=False)
        except Exception as exc:
            raise SourceError(str(exc) or "Could not open that link") from exc
        if not isinstance(info, dict):
            raise SourceError("Could not open that link")
        tracks = tracks_from_info(info, limit)
        if not tracks:
            raise SourceError("That link has no playable songs")
        title = one_line(info.get("title"), "Playlist" if kind == "playlist" else tracks[0].title)
        total = info.get("playlist_count")
        try:
            total_count = int(total) if total is not None else None
        except (TypeError, ValueError):
            total_count = None
        return title, tracks, total_count

    def _collect_spotify(self, url: str, limit: int) -> tuple[str, list[Track], int | None]:
        """List songs on a public Spotify playlist, album, or track page."""

        limit = max(1, min(SPOTIFY_PAGE_LIMIT, limit))
        target = spotify_target(url)
        if target is None:
            raise SourceError("Could not open that Spotify link")
        kind, value = target
        if kind == "link":
            final, _body = _http_get(value)
            target = spotify_target(final)
            if target is None or target[0] == "link":
                raise SourceError("Could not open that Spotify link")
            kind, value = target
        _final, html = _http_get(f"https://open.spotify.com/embed/{kind}/{value}")
        title, tracks, total = tracks_from_spotify_html(html, limit)
        return title, tracks, total

    def resolve(self, track: Track) -> Stream:
        """Return a direct audio URL. yt-dlp is called with download=False."""

        options = _base_options()
        options.update(
            {
                "format": "bestaudio[acodec!=none]/bestaudio/best",
                "noplaylist": True,
            }
        )
        if track.video_id:
            target = track.watch_url
        else:
            query = track.query or track.title
            if not query:
                raise SourceError("Could not open that track")
            target = f"ytsearch1:{query}"
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(target, download=False)
        except Exception as exc:
            raise SourceError(str(exc) or "Could not open that track") from exc
        info = _first_video(info)
        if not isinstance(info, dict):
            raise SourceError("Could not open that track")
        stream_url, headers = _audio_url(info)
        if not stream_url:
            raise SourceError("No audio stream is available for that track")
        return Stream(url=stream_url, headers=headers)


def _first_video(info: object) -> dict[str, Any] | None:
    """Use a search result's first real video, or the video dict itself."""

    if not isinstance(info, dict):
        return None
    entries = info.get("entries")
    if not isinstance(entries, list):
        return info
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        video_id = str(entry.get("id") or "")
        if video_id and not video_id.startswith("MPRE"):
            return entry
    return None


def _audio_url(info: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Pick the resolved media URL and the HTTP headers YouTube expects."""

    headers = dict(info.get("http_headers") or {})
    url = info.get("url")
    if isinstance(url, str) and url.startswith("http"):
        return url, {str(key): str(value) for key, value in headers.items()}

    formats = info.get("formats") or []
    audio_formats = [
        fmt
        for fmt in formats
        if isinstance(fmt, dict)
        and fmt.get("acodec") not in (None, "none")
        and isinstance(fmt.get("url"), str)
        and str(fmt.get("url")).startswith("http")
    ]
    if not audio_formats:
        return "", {}
    chosen = audio_formats[-1]
    chosen_headers = dict(chosen.get("http_headers") or headers)
    return str(chosen["url"]), {str(key): str(value) for key, value in chosen_headers.items()}
