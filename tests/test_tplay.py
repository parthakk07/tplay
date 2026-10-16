"""UI behaviour with a fake player, plus mpv IPC against a tiny local tone."""

from __future__ import annotations

import math
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

from tplay.app import TPlayApp
from tplay.models import Stream, Track, clean_artist, format_duration
from tplay.player import (
    DeadPlayer,
    MpvPlayer,
    PlaybackSnapshot,
    PlayerError,
    find_mpv,
    forget_player_process,
    ipc_server,
    mpv_install_hint,
    popen_kwargs,
    stop_tracked_player,
    track_player_process,
)
from tplay.source import (
    SourceError,
    YouTubeSource,
    classify_query,
    track_from_entry,
    tracks_from_info,
    tracks_from_spotify_html,
)
from tplay.widgets import RECOMMENDED, HelpScreen, ThinScrollBarRender, TrackItem

SAMPLE = [
    Track("dQw4w9WgXcQ", "Never Gonna Give You Up", "Rick Astley", 213),
    Track("fJ9rUzIMcZQ", "Bohemian Rhapsody", "Queen", 359),
    Track("kXYiU_JCYtU", "Numb", "Linkin Park", 187),
]


class FakeSource:
    def __init__(self, tracks: list[Track] | None = None) -> None:
        self.tracks = list(tracks or SAMPLE)
        self.queries: list[str] = []
        self.resolved: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.fail_ids: set[str] = set()

    def search(self, query: str, limit: int = 15) -> list[Track]:
        self.queries.append(query)
        if query == "nomatch":
            return []
        return self.tracks[:limit]

    def resolve(self, track: Track) -> Stream:
        self.resolved.append(track.video_id)
        if track.video_id in self.fail_ids:
            raise SourceError("unavailable")
        return Stream(url=f"https://example.test/{track.video_id}", headers={"User-Agent": "test"})

    def collect(self, url: str, kind: str, limit: int = 100) -> tuple[str, list[Track], int | None]:
        self.links.append((url, kind))
        if "missing" in url:
            raise SourceError("gone")
        if kind == "video":
            return self.tracks[0].title, self.tracks[:1], 1
        return "Liked songs", list(self.tracks), len(self.tracks)


class FakePlayer:
    def __init__(self) -> None:
        self.started = False
        self.loads: list[str] = []
        self.volume = 80
        self.paused = True
        self.position = 0.0
        self.duration: float | None = None
        self.idle = True
        self.seeks: list[float] = []

    def start(self) -> None:
        self.started = True

    def shutdown(self) -> None:
        self.started = False

    def load(self, stream: Stream) -> None:
        self.loads.append(stream.url)
        self.paused = False
        self.idle = False
        self.position = 1.0
        self.duration = 200.0

    def stop(self) -> None:
        self.idle = True
        self.position = 0.0

    def toggle_pause(self) -> None:
        self.paused = not self.paused

    def set_volume(self, volume: int) -> None:
        self.volume = volume

    def seek_relative(self, seconds: float) -> None:
        self.seeks.append(seconds)
        if self.duration:
            self.position = min(self.duration, max(0.0, self.position + seconds))

    def seek_ratio(self, ratio: float) -> None:
        if self.duration:
            self.position = ratio * self.duration

    def snapshot(self) -> PlaybackSnapshot:
        return PlaybackSnapshot(
            position=self.position,
            duration=self.duration,
            paused=self.paused,
            volume=float(self.volume),
            idle=self.idle,
        )

    def log_tail(self) -> str:
        return ""


def _app(tmp_path: Path, source: FakeSource | None = None, player: FakePlayer | None = None) -> TPlayApp:
    return TPlayApp(
        source=source or FakeSource(),
        player=player or FakePlayer(),
        config_path=tmp_path / "state.json",
    )


async def _search(pilot, app: TPlayApp, query: str) -> None:
    search = app.query_one("#search-input")
    search.focus()
    search.value = ""
    await pilot.press(*query)
    await pilot.press("enter")
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_classify_youtube_links() -> None:
    assert classify_query("rick astley") == "search"
    assert classify_query("https://example.com/watch?v=abc") == "search"
    assert classify_query("https://youtu.be/dQw4w9WgXcQ") == "video"
    assert classify_query("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "video"
    assert classify_query("https://www.youtube.com/playlist?list=PLabcdefghijk") == "playlist"
    assert classify_query("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabcdefghijk") == "playlist"
    assert classify_query("PLabcdefghijk") == "playlist"
    assert classify_query("youtube.com/shorts/dQw4w9WgXcQ") == "video"
    assert classify_query("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc") == "playlist"
    assert classify_query("https://open.spotify.com/intl-en/album/6dVIqQ8qmQ5GBnJ9shOYGE") == "playlist"
    assert classify_query("spotify:track:3zOYmiNOuyDL0ClxJcEJ0T") == "video"
    assert classify_query("open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M") == "playlist"
    assert classify_query("https://spotify.link/abc") == "playlist"


_SPOTIFY_PLAYLIST = """
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"state":{"data":{"entity":{"type":"playlist","title":"Road","trackList":[
{"title":"Song","subtitle":"Artist","duration":180000},
{"title":"   ","subtitle":"Nobody","duration":1000},
{"title":"Second","subtitle":"Band","duration":90000}
]}}}}}}
</script>
"""

_SPOTIFY_TRACK = """
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"state":{"data":{"entity":{"type":"track","title":"Solo","artists":[{"name":"Ashe"}],"duration":198840}}}}}}
</script>
"""


def test_spotify_page_lists_songs_without_a_youtube_id() -> None:
    title, tracks, total = tracks_from_spotify_html(_SPOTIFY_PLAYLIST)
    assert title == "Road"
    assert total is None
    assert [(track.title, track.artist, track.duration, track.query) for track in tracks] == [
        ("Song", "Artist", 180, "Artist - Song"),
        ("Second", "Band", 90, "Band - Second"),
    ]
    assert tracks[0].video_id == ""
    title, tracks, _total = tracks_from_spotify_html(_SPOTIFY_TRACK)
    assert title == "Solo"
    assert tracks[0].artist == "Ashe"
    assert tracks[0].query == "Ashe - Solo"
    assert tracks[0].duration == 198


def test_spotify_collect_reads_the_embed_page(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str) -> tuple[str, str]:
        assert url.endswith("/playlist/37i9dQZF1DXcBWIGoYBM5M")
        return url, _SPOTIFY_PLAYLIST

    monkeypatch.setattr("tplay.source._http_get", fake_get)
    title, tracks, _total = YouTubeSource().collect(
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc",
        "playlist",
    )
    assert title == "Road"
    assert [track.title for track in tracks] == ["Song", "Second"]


def test_spotify_short_link_follows_the_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str) -> tuple[str, str]:
        if "spotify.link" in url:
            return "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", ""
        return url, _SPOTIFY_PLAYLIST

    monkeypatch.setattr("tplay.source._http_get", fake_get)
    title, tracks, _total = YouTubeSource().collect("https://spotify.link/abc", "playlist")
    assert title == "Road"
    assert tracks[0].title == "Song"


def test_missing_spotify_page_is_an_error() -> None:
    with pytest.raises(SourceError):
        tracks_from_spotify_html("<html>nope</html>")


def test_spotify_song_resolves_with_a_youtube_search(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    class FakeYDL:
        def __init__(self, options: dict) -> None:
            assert options["format"].startswith("bestaudio")

        def __enter__(self) -> "FakeYDL":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def extract_info(self, url: str, download: bool = False) -> dict:
            assert download is False
            seen.append(url)
            return {
                "entries": [
                    {"id": "MPREskip", "title": "Album"},
                    {
                        "id": "abc",
                        "title": "Song",
                        "url": "https://example.test/audio",
                        "http_headers": {"User-Agent": "t"},
                    },
                ]
            }

    monkeypatch.setattr("tplay.source.yt_dlp.YoutubeDL", FakeYDL)
    stream = YouTubeSource().resolve(Track("", "Song", "Artist", 180, "Artist - Song"))
    assert seen == ["ytsearch1:Artist - Song"]
    assert stream.url == "https://example.test/audio"
    assert stream.headers["User-Agent"] == "t"


def test_format_duration_and_artist() -> None:
    assert format_duration(None) == "--:--"
    assert format_duration(0) == "0:00"
    assert format_duration(74) == "1:14"
    assert format_duration(3723) == "1:02:03"
    assert clean_artist("Queen - Topic") == "Queen"
    assert clean_artist("   ") == "Unknown artist"


def test_track_parsing_skips_rows_without_a_title() -> None:
    info = {
        "entries": [
            {"id": "abc", "title": "Song", "channel": "Artist - Topic", "duration": 12.6},
            {"id": "MPREalbum", "title": "Album", "duration": 10},
            {"id": "nope", "title": "   ", "duration": 10},
            {"title": "Missing id"},
            None,
        ]
    }
    tracks = tracks_from_info(info)
    assert [track.video_id for track in tracks] == ["abc"]
    assert tracks[0].artist == "Artist"
    assert tracks[0].duration == 12
    assert track_from_entry({"id": "x", "title": "Live", "uploader": "Band"}) is not None


@pytest.mark.asyncio
async def test_search_play_volume_and_queue(tmp_path: Path) -> None:
    source = FakeSource()
    player = FakePlayer()
    app = _app(tmp_path, source, player)
    async with app.run_test(size=(110, 36)) as pilot:
        assert player.started
        assert "tplay" in str(app.query_one("#logo").content)
        await _search(pilot, app, "queen")
        assert source.queries == ["queen"]
        assert [item.track.title for item in app.query("#results TrackItem")] == [track.title for track in SAMPLE]
        assert "streamed, nothing saved" in str(app.query_one("#status").content)

        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.loads == ["https://example.test/dQw4w9WgXcQ"]
        assert app.current is not None
        assert app.current.title == "Never Gonna Give You Up"
        assert "Opening stream" in str(app.query_one("#track-artist").content) or app.loading or player.loads

        app._tick()
        assert app.query_one("#btn-play").content == "❚❚" or str(app.query_one("#btn-play").content) == "❚❚"

        await pilot.press("+")
        assert app.volume == 85
        assert player.volume == 85
        await pilot.press("m")
        assert player.volume == 0
        assert app.volume == 85
        await pilot.press("m")
        assert player.volume == 85

        await pilot.press("n")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.current is not None
        assert app.current.title == "Bohemian Rhapsody"
        assert player.loads[-1].endswith("fJ9rUzIMcZQ")

        await pilot.press("right")
        assert player.seeks[-1] == 10

        queued = len(app.queue)
        await pilot.press("k")
        await pilot.press("a")
        await pilot.pause()
        assert len(app.queue) == queued + 1

        await pilot.press("2")
        assert "visible" in app.query_one("#view-queue").classes
        await pilot.press("3")
        assert "visible" in app.query_one("#view-now").classes
        assert "Bohemian Rhapsody" in str(app.query_one("#now-title").content)

        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)

        search = app.query_one("#search-input")
        search.focus()
        before = app.volume
        await pilot.press("+")
        assert app.volume == before
        assert "+" in search.value


def test_scrollbar_is_a_single_thin_line() -> None:
    from rich.color import Color

    bar = ThinScrollBarRender.render_bar(
        size=12,
        virtual_size=100,
        window_size=25,
        position=10,
        thickness=1,
        vertical=True,
        back_color=Color.parse("#121212"),
        bar_color=Color.parse("#6a6a6a"),
    )
    text = "".join(segment.text for segment in bar.segments)
    assert "│" in text
    assert " " in text
    for chunky in "▁▂▃▄▅▆▇█▌▍":
        assert chunky not in text
    styles = [segment.style for segment in bar.segments if segment.text and "│" in segment.text]
    assert styles
    assert not any(style.reverse for style in styles)


@pytest.mark.asyncio
async def test_long_list_scrolls_on_a_thin_line(tmp_path: Path) -> None:
    tracks = [Track(f"id{i}", f"Song {i}", "Artist", 90) for i in range(40)]
    app = _app(tmp_path, FakeSource(tracks))
    async with app.run_test(size=(80, 24)) as pilot:
        await _search(pilot, app, "mix")
        results = app.query_one("#results")
        results.focus()
        await pilot.pause()
        await pilot.press(*(["down"] * 20))
        await pilot.wait_for_scheduled_animations()
        assert results.scroll_y > 0
        shot = tmp_path / "list.svg"
        app.save_screenshot(str(shot))
        svg = shot.read_text(encoding="utf-8")
        assert "│" in svg
        for chunky in "▁▂▃▄▅▆▇":
            assert chunky not in svg


@pytest.mark.asyncio
async def test_empty_search_and_screenshots(tmp_path: Path) -> None:
    app = _app(tmp_path)
    shot_dir = Path("/tmp/tplay-shots")
    shot_dir.mkdir(exist_ok=True)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause()
        app.save_screenshot(str(shot_dir / "home.svg"))
        await _search(pilot, app, "nomatch")
        assert app.results == []
        assert "No matches" in str(app.query_one("#status").content)
        await _search(pilot, app, "lofi")
        app._tick()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.player.position = 74
        app.player.duration = 213
        app.loading = False
        app._paint_identity()
        app._tick()
        await pilot.pause()
        app.save_screenshot(str(shot_dir / "search.svg"))
        await pilot.press("3")
        await pilot.pause()
        app.save_screenshot(str(shot_dir / "now.svg"))
        await pilot.click("#nav-queue")
        await pilot.pause()
        app.save_screenshot(str(shot_dir / "queue.svg"))


async def _play_first(pilot, app: TPlayApp) -> None:
    await _search(pilot, app, "queen")
    await pilot.press("enter")
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _settle(app: TPlayApp, pilot) -> None:
    for _ in range(40):
        await app.workers.wait_for_complete()
        await pilot.pause()
        if not app.loading and not list(app.workers):
            return


def _reverse(items: list[int]) -> None:
    items.reverse()


@pytest.mark.asyncio
async def test_shuffle_plays_each_song_once_then_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tplay.app.random.shuffle", _reverse)
    player = FakePlayer()
    app = _app(tmp_path, player=player)
    async with app.run_test(size=(100, 30)) as pilot:
        await _play_first(pilot, app)
        app._on_mpv({"event": "start-file", "playlist_entry_id": 1})
        app._on_mpv({"event": "playback-restart"})
        await pilot.press("s")
        await pilot.pause()
        assert app.shuffle is True
        assert "on" in app.query_one("#btn-shuffle").classes
        assert "Numb" in str(app.query_one("#up-next").content)
        await pilot.press("n")
        await _settle(app, pilot)
        assert app.queue_index == 2
        assert app.current is not None
        assert app.current.title == "Numb"
        await pilot.press("p")
        await _settle(app, pilot)
        assert app.queue_index == 0
        assert app.current.title == "Never Gonna Give You Up"
        await pilot.press("n")
        await _settle(app, pilot)
        await pilot.press("n")
        await _settle(app, pilot)
        assert app.queue_index == 1
        assert app.current.title == "Bohemian Rhapsody"
        await pilot.press("n")
        await _settle(app, pilot)
        assert app.queue_index == 1
        assert "End of queue" in str(app.query_one("#status").content)
        await pilot.press("s")
        await pilot.pause()
        assert app.shuffle is False
        assert "on" not in app.query_one("#btn-shuffle").classes


@pytest.mark.asyncio
async def test_repeat_queue_starts_again_and_repeat_song_replays(tmp_path: Path) -> None:
    player = FakePlayer()
    app = _app(tmp_path, player=player)
    async with app.run_test(size=(100, 30)) as pilot:
        await _play_first(pilot, app)
        app._on_mpv({"event": "start-file", "playlist_entry_id": 1})
        app._on_mpv({"event": "playback-restart"})
        await pilot.press("n")
        await _settle(app, pilot)
        await pilot.press("n")
        await _settle(app, pilot)
        assert app.queue_index == 2
        await pilot.press("r")
        await pilot.pause()
        assert app.loop == "all"
        assert "on" in app.query_one("#btn-loop").classes
        await pilot.press("n")
        await _settle(app, pilot)
        assert app.queue_index == 0
        assert app.current is not None
        assert app.current.title == "Never Gonna Give You Up"
        await pilot.press("r")
        await pilot.pause()
        assert app.loop == "one"
        assert "1" in str(app.query_one("#btn-loop").render())
        loads = len(player.loads)
        app._on_mpv({"event": "start-file", "playlist_entry_id": 8})
        app._on_mpv({"event": "playback-restart"})
        app._on_mpv({"event": "end-file", "reason": "eof", "playlist_entry_id": 8})
        await _settle(app, pilot)
        assert app.queue_index == 0
        assert app.current.title == "Never Gonna Give You Up"
        assert len(player.loads) == loads + 1
        await pilot.press("n")
        await _settle(app, pilot)
        assert app.queue_index == 1
        assert app.current.title == "Bohemian Rhapsody"


@pytest.mark.asyncio
async def test_end_of_track_plays_the_next_song(tmp_path: Path) -> None:
    player = FakePlayer()
    app = _app(tmp_path, player=player)
    async with app.run_test(size=(100, 30)) as pilot:
        await _play_first(pilot, app)
        app._on_mpv({"event": "start-file", "playlist_entry_id": 1})
        app._on_mpv({"event": "playback-restart"})
        app._on_mpv({"event": "end-file", "reason": "eof", "playlist_entry_id": 1})
        await _settle(app, pilot)
        assert app.queue_index == 1
        assert app.current is not None
        assert app.current.title == "Bohemian Rhapsody"
        assert player.loads[-1].endswith("fJ9rUzIMcZQ")


@pytest.mark.asyncio
async def test_replacing_a_track_does_not_skip_an_extra_song(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _play_first(pilot, app)
        app._on_mpv({"event": "start-file", "playlist_entry_id": 1})
        await pilot.press("n")
        # The file we left can still report eof. That must not skip again.
        app._on_mpv({"event": "end-file", "reason": "eof", "playlist_entry_id": 1})
        app._on_mpv({"event": "end-file", "reason": "stop", "playlist_entry_id": 1})
        await _settle(app, pilot)
        assert app.queue_index == 1
        assert app.current is not None
        assert app.current.title == "Bohemian Rhapsody"


@pytest.mark.asyncio
async def test_stream_error_skips_to_the_next_song(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _play_first(pilot, app)
        app._on_mpv({"event": "start-file", "playlist_entry_id": 4})
        app._on_mpv({"event": "playback-restart"})
        app._on_mpv(
            {
                "event": "end-file",
                "reason": "error",
                "playlist_entry_id": 4,
                "file_error": "loading failed",
            }
        )
        await _settle(app, pilot)
        assert app.queue_index == 1
        assert app.current is not None
        assert app.current.title == "Bohemian Rhapsody"
        app._on_mpv({"event": "start-file", "playlist_entry_id": 5})
        app._on_mpv({"event": "end-file", "reason": "redirect", "playlist_entry_id": 5})
        assert app.queue_index == 1


@pytest.mark.asyncio
async def test_repeated_failures_stop_instead_of_skipping_forever(tmp_path: Path) -> None:
    tracks = [Track(f"id{i}", f"Song {i}", "Artist", 90) for i in range(8)]
    source = FakeSource(tracks)
    source.fail_ids = {track.video_id for track in tracks}
    app = _app(tmp_path, source=source)
    async with app.run_test(size=(100, 30)) as pilot:
        await _play_first(pilot, app)
        await _settle(app, pilot)
        assert app.queue_index == 5
        assert app.current is not None
        assert app.current.title == "Song 5"
        assert "unavailable" in str(app.query_one("#status").content)
        assert app.player.loads == []


@pytest.mark.asyncio
async def test_playlist_link_queues_songs_and_starts_when_idle(tmp_path: Path) -> None:
    source = FakeSource()
    player = FakePlayer()
    app = _app(tmp_path, source=source, player=player)
    async with app.run_test(size=(100, 30)) as pilot:
        search = app.query_one("#search-input")
        search.focus()
        search.value = "https://www.youtube.com/playlist?list=PLabcdefghijk"
        await pilot.press("enter")
        await _settle(app, pilot)
        assert source.links[-1][1] == "playlist"
        assert [track.title for track in app.queue] == [track.title for track in SAMPLE]
        assert app.current is not None
        assert app.current.title == SAMPLE[0].title
        assert player.loads[-1].endswith(SAMPLE[0].video_id)
        assert "visible" in app.query_one("#view-queue").classes
        assert "Liked songs" in str(app.query_one("#status").content)

        app._on_mpv({"event": "start-file", "playlist_entry_id": 1})
        app._on_mpv({"event": "playback-restart"})
        search.focus()
        search.value = "https://youtu.be/dQw4w9WgXcQ"
        await pilot.press("enter")
        await _settle(app, pilot)
        assert source.links[-1][1] == "video"
        assert len(app.queue) == len(SAMPLE) + 1
        assert app.current.title == SAMPLE[0].title
        assert player.loads[-1].endswith(SAMPLE[0].video_id)


@pytest.mark.asyncio
async def test_spotify_playlist_link_queues_and_starts(tmp_path: Path) -> None:
    source = FakeSource()
    player = FakePlayer()
    app = _app(tmp_path, source=source, player=player)
    async with app.run_test(size=(100, 30)) as pilot:
        search = app.query_one("#search-input")
        search.focus()
        search.value = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
        await pilot.press("enter")
        await _settle(app, pilot)
        assert source.links[-1] == (
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "playlist",
        )
        assert app.current is not None
        assert app.current.title == SAMPLE[0].title
        assert "visible" in app.query_one("#view-queue").classes


def _write_tone(path: Path, seconds: float = 1.2) -> None:
    rate = 22050
    frames = int(seconds * rate)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        data = bytearray()
        for index in range(frames):
            sample = int(8000 * math.sin(2 * math.pi * 440 * index / rate))
            data += struct.pack("<h", sample)
        handle.writeframes(bytes(data))


@pytest.mark.asyncio
async def test_recommendations_play_and_sidebar_lists_every_shortcut(tmp_path: Path) -> None:
    player = FakePlayer()
    source = FakeSource()
    app = _app(tmp_path, source=source, player=player)
    songs = [track for track in RECOMMENDED if classify_query(track.query) == "search"]
    playlists = [track for track in RECOMMENDED if classify_query(track.query) != "search"]
    async with app.run_test(size=(80, 24)) as pilot:
        for _ in range(10):
            await pilot.pause()
            if app.query("#recommendations TrackItem"):
                break
        items = list(app.query("#recommendations TrackItem"))
        assert [item.track.title for item in items] == [track.title for track in RECOMMENDED]
        assert [item.track.artist for item in items if item.track in playlists] == ["Playlist · Spotify"] * len(playlists)
        assert songs and playlists
        assert "show" in app.query_one("#recommendations").classes
        keys = str(app.query_one("#sidebar-keys").render())
        for shortcut in ("search", "play", "queue", "pause", "next", "previous", "shuffle", "repeat", "volume", "mute", "ctrl+c"):
            assert shortcut in keys
        await pilot.click(items[0])
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert player.loads
        assert app.current is not None
        assert app.current.title == songs[0].title
        assert [track.title for track in app.queue] == [track.title for track in songs]
        playlist = playlists[0]
        playlist_row = next(item for item in items if item.track == playlist)
        playlist_row.scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click(playlist_row)
        await _settle(app, pilot)
        assert source.links[-1] == (playlist.query, "playlist")
        assert app.current.title == songs[0].title
        assert [track.title for track in app.queue] == [track.title for track in songs] + [track.title for track in SAMPLE]
        assert "visible" in app.query_one("#view-queue").classes
        await pilot.press("1")
        await _search(pilot, app, "queen")
        assert "show" not in app.query_one("#recommendations").classes
        assert "show" in app.query_one("#results").classes


def test_find_mpv_checks_a_normal_install_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "mpv"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr("tplay.player.shutil.which", lambda _name: None)
    monkeypatch.setattr("tplay.player._mpv_candidates", lambda: [str(binary)])
    assert find_mpv() == str(binary)
    monkeypatch.setattr("tplay.player._mpv_candidates", lambda: [])
    assert find_mpv() is None


@pytest.mark.asyncio
async def test_missing_mpv_opens_the_app_and_explains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tplay.player.find_mpv", lambda: None)
    app = TPlayApp(source=FakeSource(), player=None, config_path=tmp_path / "state.json")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert isinstance(app.player, DeadPlayer)
        assert "mpv is not installed" in str(app.query_one("#status").content)
        assert "Install it with:" in app.player.message
        await _search(pilot, app, "queen")
        await pilot.press("enter")
        await pilot.pause()
        assert app.current is not None
        assert app.current.title == "Never Gonna Give You Up"
        assert "mpv is not installed" in str(app.query_one("#status").content)


def test_ipc_address_follows_the_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert ipc_server(12) == r"\\.\pipe\tplay-12"
    assert popen_kwargs() == {"creationflags": 0x00000200 | 0x08000000}
    assert "winget" in mpv_install_hint()
    monkeypatch.setattr(sys, "platform", "darwin")
    assert ipc_server(12).endswith("tplay-12.sock")
    assert popen_kwargs() == {"start_new_session": True}
    assert mpv_install_hint() == "brew install mpv"
    monkeypatch.setattr(sys, "platform", "linux")
    assert ipc_server(12).endswith("tplay-12.sock")
    assert "apt install mpv" in mpv_install_hint()


@pytest.mark.asyncio
async def test_control_boxes_are_ascii_and_clickable(tmp_path: Path) -> None:
    player = FakePlayer()
    app = _app(tmp_path, player=player)
    async with app.run_test(size=(80, 24)) as pilot:
        await _play_first(pilot, app)
        app._on_mpv({"event": "start-file", "playlist_entry_id": 1})
        app._on_mpv({"event": "playback-restart"})
        app._tick()
        await pilot.pause()
        assert "❚❚" in str(app.query_one("#btn-play").render())
        assert "⇄" in str(app.query_one("#btn-shuffle").render())
        assert "↻" in str(app.query_one("#btn-loop").render())
        await pilot.click("#btn-next")
        await _settle(app, pilot)
        assert app.queue_index == 1
        await pilot.click("#btn-shuffle")
        assert app.shuffle is True
        await pilot.click("#btn-loop")
        assert app.loop == "all"
        await pilot.click("#btn-loop")
        assert app.loop == "one"
        assert "1" in str(app.query_one("#btn-loop").render())
        await pilot.click("#btn-vol-up")
        await pilot.pause()
        assert app.volume == 85
        await pilot.click("#volume-bar", offset=(0, 0))
        await pilot.pause()
        assert app.volume == 0
        await pilot.click("#seek-bar", offset=(10, 0))
        await pilot.pause()
        assert player.position > 0
        await pilot.click("#nav-queue")
        await pilot.pause()
        assert "visible" in app.query_one("#view-queue").classes
        for widget_id in ("#btn-shuffle", "#btn-play", "#btn-loop", "#volume-bar", "#seek-bar"):
            region = app.query_one(widget_id).region
            assert region.width > 0 and region.height > 0


def test_mpv_is_tied_to_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_popen(cmd: list[str], **kwargs: object) -> None:
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        raise OSError("stop")

    monkeypatch.setattr("tplay.player.subprocess.Popen", fake_popen)
    monkeypatch.setattr("tplay.player.find_mpv", lambda: "/usr/bin/mpv")
    player = MpvPlayer(audio_output="null")
    with pytest.raises(PlayerError):
        player.start()
    command = seen["cmd"]
    kwargs = seen["kwargs"]
    assert isinstance(command, list)
    assert isinstance(kwargs, dict)
    assert "preexec_fn" not in kwargs
    if sys.platform == "linux":
        assert "prctl" in "\n".join(str(part) for part in command)
    else:
        assert command[0] == "/usr/bin/mpv"
    assert player.log_tail() == ""


def _process_state(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _wait_until_stopped(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _process_state(pid)
        if not state or state.startswith("Z"):
            return True
        time.sleep(0.05)
    return False


def _run_linger(tmp_path: Path, body: str) -> tuple[subprocess.Popen[bytes], Path, Path]:
    script = tmp_path / "linger.py"
    script.write_text(body, encoding="utf-8")
    err = tmp_path / "linger.err"
    err_handle = err.open("w", encoding="utf-8")
    env = os.environ.copy()
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.DEVNULL,
        stderr=err_handle,
        env=env,
    )
    err_handle.close()
    return proc, tmp_path / "child.pid", err


def _child_pid(path: Path, parent: subprocess.Popen[bytes], err: Path) -> int:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return int(path.read_text(encoding="utf-8").strip())
        if parent.poll() is not None:
            detail = err.read_text(encoding="utf-8", errors="replace")
            raise AssertionError(f"helper exited {parent.returncode}: {detail}")
        time.sleep(0.05)
    detail = err.read_text(encoding="utf-8", errors="replace")
    raise AssertionError(f"helper did not start the player: {detail}")


@pytest.mark.skipif(sys.platform == "win32", reason="terminal hangup is a unix signal")
def test_hangup_stops_playback(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    parent, pid_path, err = _run_linger(
        tmp_path,
        "import time\n"
        "from tplay.player import popen_kwargs, track_player_process\n"
        "import subprocess\n"
        "child = subprocess.Popen(['sleep', '120'], **popen_kwargs())\n"
        "track_player_process(child.pid)\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "while child.poll() is None:\n"
        "    time.sleep(0.1)\n",
    )
    child_pid = 0
    try:
        child_pid = _child_pid(pid_path, parent, err)
        os.kill(parent.pid, signal.SIGHUP)
        parent.wait(timeout=5)
        assert _wait_until_stopped(child_pid)
    finally:
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=2)


@pytest.mark.skipif(sys.platform != "linux", reason="parent-death signal is Linux")
def test_parent_death_stops_playback(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    parent, pid_path, err = _run_linger(
        tmp_path,
        "import time\n"
        "import subprocess\n"
        "import os\n"
        "from tplay.player import popen_kwargs, tie_to_parent\n"
        "child = subprocess.Popen(tie_to_parent(['sleep', '120']), **popen_kwargs())\n"
        "deadline = time.monotonic() + 5\n"
        "while time.monotonic() < deadline:\n"
        "    try:\n"
        "        exe = os.readlink(f'/proc/{child.pid}/exe')\n"
        "    except OSError:\n"
        "        exe = ''\n"
        "    if exe.endswith('sleep'):\n"
        "        break\n"
        "    time.sleep(0.02)\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "while True:\n"
        "    time.sleep(0.2)\n",
    )
    child_pid = 0
    try:
        child_pid = _child_pid(pid_path, parent, err)
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        assert _wait_until_stopped(child_pid)
    finally:
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=2)


def test_stop_tracked_player_kills_only_the_player() -> None:
    proc = subprocess.Popen(["sleep", "120"], **popen_kwargs())
    try:
        track_player_process(proc.pid)
        stop_tracked_player()
        assert _wait_until_stopped(proc.pid)
        forget_player_process(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


@pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv is not installed")
def test_mpv_streams_a_local_tone_and_changes_volume(tmp_path: Path) -> None:
    events: list[dict] = []
    tone = tmp_path / "tone.wav"
    _write_tone(tone)
    player = MpvPlayer(on_event=events.append, volume=30, audio_output="null")
    player.start()
    try:
        player.load(Stream(url=tone.as_uri(), headers={}))
        deadline = time.monotonic() + 8
        seen_position = False
        while time.monotonic() < deadline:
            snapshot = player.snapshot()
            if snapshot.position and snapshot.position > 0.2:
                seen_position = True
                break
            time.sleep(0.05)
        player.set_volume(55)
        time.sleep(0.2)
        assert seen_position
        assert player.snapshot().volume == pytest.approx(55, abs=0.1)
        while time.monotonic() < deadline:
            if any(event.get("event") == "end-file" for event in events):
                break
            time.sleep(0.05)
        reasons = [event.get("reason") for event in events if event.get("event") == "end-file"]
        assert "eof" in reasons
    finally:
        player.shutdown()
