"""Spotify-style terminal player. Search, queue, volume, and streamed playback."""

from __future__ import annotations

import asyncio
import json
import os
import random
from functools import partial
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Input, ListView, Static

from tplay.models import Stream, Track, art_color, format_duration, short_error
from tplay.player import DeadPlayer, MpvPlayer, PlaybackSnapshot
from tplay.source import YouTubeSource, classify_query
from tplay.widgets import (
    HelpScreen,
    NowView,
    PlayerBar,
    QueueView,
    SearchView,
    SeekBar,
    Sidebar,
    TrackItem,
    VolumeBar,
)


VOLUME_STEP = 5
SKIP_LIMIT = 5


def default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg) if xdg else Path.home() / ".config"
    return root / "tplay" / "state.json"


def load_volume(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = int(data["volume"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return 80
    return max(0, min(100, value))


def save_volume(path: Path, volume: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"volume": int(volume)}) + "\n", encoding="utf-8")
    except OSError:
        return


class TPlayApp(App[None]):
    """Full-screen terminal player. Audio is streamed and never saved."""

    TITLE = "tplay"
    ENABLE_COMMAND_PALETTE = False
    NOTIFICATION_TIMEOUT = 3

    CSS = """
    Screen {
        layout: vertical;
        background: #121212;
        color: #ffffff;
    }
    #body {
        height: 1fr;
        width: 100%;
        background: #121212;
    }
    #sidebar {
        width: 28;
        height: 100%;
        background: #000000;
        padding: 1 0 0 0;
    }
    #logo {
        height: 2;
        padding: 0 2;
        text-style: bold;
        content-align: left bottom;
    }
    #tagline {
        height: 2;
        padding: 0 2;
        color: #6a6a6a;
    }
    .nav {
        height: 3;
        width: 100%;
        padding: 0 1 0 1;
        color: #b3b3b3;
        background: #000000;
        content-align: left middle;
        border-left: tall #000000;
    }
    .nav.active {
        color: #ffffff;
        text-style: bold;
        border-left: tall #1db954;
    }
    .nav:hover {
        color: #ffffff;
        background: #121212;
    }
    #sidebar-gap {
        height: 1fr;
    }
    #sidebar-keys {
        height: 6;
        padding: 0 2 1 2;
        color: #5c5c5c;
    }
    #main {
        width: 1fr;
        height: 100%;
        background: #121212;
    }
    .view {
        display: none;
        height: 1fr;
        padding: 1 2 0 2;
        background: #121212;
    }
    .view.visible {
        display: block;
    }
    .page-title {
        height: 1;
        color: #ffffff;
        text-style: bold;
    }
    #search-input {
        margin: 1 0;
        height: 3;
        border: round #333333;
        background: #242424;
        color: #ffffff;
        padding: 0 1;
    }
    #search-input:focus {
        border: round #ffffff;
        background: #2a2a2a;
    }
    #search-input .input--placeholder {
        color: #8a8a8a;
    }
    #search-input .input--cursor {
        background: #1db954;
        color: #000000;
    }
    #status {
        height: 1;
        color: #b3b3b3;
        text-overflow: ellipsis;
    }
    #status.error {
        color: #ff6b6b;
    }
    #queue-hint {
        height: 1;
        color: #6a6a6a;
        margin: 1 0 0 0;
    }
    #results, #queue-list, #empty-search, #empty-queue {
        display: none;
        height: 1fr;
    }
    #results.show, #queue-list.show, #empty-search.show, #empty-queue.show {
        display: block;
    }
    #empty-search, #empty-queue {
        color: #b3b3b3;
        content-align: center middle;
        text-align: center;
    }
    ListView {
        background: #121212;
        border: none;
        padding: 0;
        scrollbar-color: #6a6a6a;
        scrollbar-color-hover: #b3b3b3;
        scrollbar-color-active: #ffffff;
        scrollbar-background: #121212;
        scrollbar-background-hover: #121212;
        scrollbar-background-active: #121212;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
    }
    ListView:focus {
        background-tint: transparent !important;
    }
    ListView > TrackItem {
        layout: horizontal;
        height: 2;
        padding: 0 1;
        background: #121212;
        overflow: hidden hidden;
    }
    ListView > TrackItem .marker {
        width: 4;
        height: 2;
        content-align: center middle;
        color: #6a6a6a;
    }
    ListView > TrackItem .track-text {
        width: 1fr;
        height: 2;
        padding: 0 1;
    }
    ListView > TrackItem .title {
        height: 1;
        color: #ffffff;
        text-overflow: ellipsis;
    }
    ListView > TrackItem .artist {
        height: 1;
        color: #b3b3b3;
        text-overflow: ellipsis;
    }
    ListView > TrackItem .length {
        width: 8;
        height: 2;
        content-align: right middle;
        color: #b3b3b3;
    }
    ListView > TrackItem.-hovered {
        background: #1a1a1a;
    }
    ListView > TrackItem.-highlight,
    ListView:focus > TrackItem.-highlight {
        background: #282828 !important;
        color: #ffffff !important;
        text-style: none !important;
    }
    ListView > TrackItem.playing .title,
    ListView > TrackItem.playing .marker {
        color: #1db954;
    }
    #now-body {
        height: 1fr;
        overflow-x: hidden;
        overflow-y: auto;
        scrollbar-color: #6a6a6a;
        scrollbar-color-hover: #b3b3b3;
        scrollbar-color-active: #ffffff;
        scrollbar-background: #121212;
        scrollbar-background-hover: #121212;
        scrollbar-background-active: #121212;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
    }
    #art-row {
        height: 8;
        align: center middle;
    }
    #big-art {
        width: 16;
        height: 8;
        content-align: center middle;
        text-style: bold;
        background: #282828;
        color: #ffffff;
    }
    #now-title {
        width: 100%;
        height: 1;
        text-align: center;
        text-style: bold;
        color: #ffffff;
        text-overflow: ellipsis;
    }
    #now-artist {
        width: 100%;
        height: 1;
        text-align: center;
        color: #b3b3b3;
        text-overflow: ellipsis;
    }
    #now-note {
        width: 100%;
        height: 1;
        margin: 1 0 0 0;
        text-align: center;
        color: #6a6a6a;
    }
    #up-next-label {
        width: 100%;
        height: 1;
        margin: 1 0 0 0;
        text-align: center;
        text-style: bold;
        color: #ffffff;
    }
    #up-next {
        width: 100%;
        height: auto;
        max-height: 4;
        text-align: center;
        color: #b3b3b3;
    }
    #player {
        height: 6;
        width: 100%;
        background: #181818;
        border-top: solid #282828;
        padding: 0 1;
        overflow: hidden hidden;
    }
    #player-top {
        height: 4;
    }
    #player-bottom {
        height: 1;
    }
    #player-meta {
        width: 1fr;
        height: 4;
        padding: 1 1 0 1;
    }
    #track-title {
        height: 1;
        color: #ffffff;
        text-style: bold;
        text-overflow: ellipsis;
    }
    #track-artist {
        height: 1;
        color: #b3b3b3;
        text-overflow: ellipsis;
    }
    #transport {
        width: 24;
        height: 4;
        align: center middle;
    }
    .transport-btn {
        width: 5;
        height: 3;
        content-align: center middle;
        color: #ffffff;
        background: transparent;
    }
    .transport-btn:hover {
        color: #1db954;
    }
    #btn-play {
        background: #ffffff;
        color: #000000;
        text-style: bold;
    }
    #btn-play:hover {
        background: #1ed760;
        color: #000000;
    }
    .transport-btn.dim {
        color: #4d4d4d;
    }
    #btn-shuffle, #btn-loop {
        width: 4;
        color: #6a6a6a;
    }
    #btn-shuffle.on, #btn-loop.on {
        color: #1db954;
    }
    #volume-box {
        width: 22;
        height: 4;
        align: right middle;
    }
    .vol-btn {
        width: 3;
        height: 3;
        content-align: center middle;
        color: #b3b3b3;
        background: transparent;
    }
    .vol-btn:hover {
        color: #ffffff;
    }
    #volume-bar {
        width: 10;
        height: 1;
    }
    #volume-readout {
        width: 4;
        height: 1;
        content-align: right middle;
        color: #b3b3b3;
    }
    #time-pos, #time-dur {
        width: 8;
        height: 1;
        content-align: center middle;
        color: #b3b3b3;
    }
    #seek-bar {
        width: 1fr;
        height: 1;
    }
    HelpScreen {
        align: center middle;
    }
    #help-card {
        width: 68;
        height: auto;
        background: #181818;
        color: #e8e8e8;
        border: round #333333;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("question_mark", "help", "Help", show=False),
        Binding("slash", "focus_search", "Search", show=False),
        Binding("space", "toggle_pause", "Play/Pause", show=False),
        Binding("n", "next_track", "Next", show=False),
        Binding("p", "previous_track", "Previous", show=False),
        Binding("s", "toggle_shuffle", "Shuffle", show=False),
        Binding("r", "toggle_loop", "Repeat", show=False),
        Binding("plus,equals,right_square_bracket", "volume_up", "Volume up", show=False),
        Binding("minus,left_square_bracket", "volume_down", "Volume down", show=False),
        Binding("m", "toggle_mute", "Mute", show=False),
        Binding("a", "add_to_queue", "Queue", show=False),
        Binding("left", "seek_back", "Seek back", show=False),
        Binding("right", "seek_forward", "Seek forward", show=False),
        Binding("shift+left", "seek_back_long", show=False),
        Binding("shift+right", "seek_forward_long", show=False),
        Binding("1", "show_search", show=False),
        Binding("2", "show_queue", show=False),
        Binding("3", "show_now", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def __init__(
        self,
        source: YouTubeSource | None = None,
        player: object | None = None,
        config_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.source = source or YouTubeSource()
        self.player = player
        self.config_path = config_path or default_config_path()
        self.volume = load_volume(self.config_path)
        self.muted = False
        self._volume_before_mute = self.volume or 40
        self.results: list[Track] = []
        self.queue: list[Track] = []
        self.queue_index: int | None = None
        self.shuffle = False
        self.loop = "off"
        self._loop_label = "↻"
        self._order: list[int] = []
        self.current: Track | None = None
        self.loading = False
        self._search_generation = 0
        self._play_generation = 0
        self._play_label = ""
        self._entry_id: object | None = None
        self._skip_burst = 0
        self._ui_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield Sidebar()
            with Vertical(id="main"):
                yield SearchView()
                yield QueueView()
                yield NowView()
        yield PlayerBar()

    def on_mount(self) -> None:
        self.theme = "textual-dark"
        self._boot_player()
        self._paint_identity()
        self._paint_volume()
        self._update_empty_states()
        self.set_interval(0.25, self._tick)
        self.query_one("#search-input", Input).focus()

    def on_unmount(self) -> None:
        self._shutdown_player()

    def action_help_quit(self) -> None:
        self.action_quit()

    def action_quit(self) -> None:
        self._shutdown_player()
        self.exit()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_focus_search(self) -> None:
        self._show_view("search")
        self.query_one("#search-input", Input).focus()

    def action_show_search(self) -> None:
        self._show_view("search")

    def action_show_queue(self) -> None:
        self._show_view("queue")

    def action_show_now(self) -> None:
        self._show_view("now")

    def action_toggle_pause(self) -> None:
        if self.player is None or self.current is None:
            return
        self.player.toggle_pause()

    def action_volume_up(self) -> None:
        if self.muted:
            self.muted = False
        self._set_volume(self.volume + VOLUME_STEP)

    def action_volume_down(self) -> None:
        self._set_volume(self.volume - VOLUME_STEP)
        if self.volume == 0:
            self.muted = True
            self._paint_volume()

    def action_toggle_mute(self) -> None:
        if self.muted or self.volume == 0:
            self.muted = False
            if self.volume == 0:
                self.volume = self._volume_before_mute or 40
            self._apply_volume()
        else:
            self._volume_before_mute = self.volume
            self.muted = True
            if self.player is not None:
                self.player.set_volume(0)
        self._paint_volume()

    def action_seek_back(self) -> None:
        self._seek(-10)

    def action_seek_forward(self) -> None:
        self._seek(10)

    def action_seek_back_long(self) -> None:
        self._seek(-30)

    def action_seek_forward_long(self) -> None:
        self._seek(30)

    def action_toggle_shuffle(self) -> None:
        self.shuffle = not self.shuffle
        if self.shuffle:
            self._build_order(self.queue_index)
        else:
            self._order = []
        self.notify("Shuffle on" if self.shuffle else "Shuffle off")
        try:
            self._paint_shuffle()
            if self.current is not None:
                self._paint_up_next()
            if self.player is not None:
                self._paint_skip_buttons(self.player.snapshot())
        except NoMatches:
            return

    def action_toggle_loop(self) -> None:
        """Cycle off, then the whole queue, then the current song."""

        order = ("off", "all", "one")
        self.loop = order[(order.index(self.loop) + 1) % len(order)]
        self.notify(
            {"off": "Repeat off", "all": "Repeat queue", "one": "Repeat song"}[self.loop]
        )
        try:
            self._paint_loop()
            if self.player is not None:
                self._paint_skip_buttons(self.player.snapshot())
        except NoMatches:
            return

    def action_next_track(self) -> None:
        self._skip_burst = 0
        self._advance(1, auto=False)

    def action_previous_track(self) -> None:
        if self.player is None:
            return
        snapshot = self.player.snapshot()
        if snapshot.position and snapshot.position > 3 and self.current is not None:
            self.player.seek_ratio(0)
            return
        self._advance(-1, auto=False)

    def action_add_to_queue(self) -> None:
        track = self._highlighted_track()
        if track is None:
            return
        self.queue.append(track)
        added = len(self.queue) - 1
        self.notify(f"Added to queue: {track.title[:80]}")
        if self.current is None:
            self.queue_index = added
            if self.shuffle:
                self._build_order(added)
            self._begin_playback(track)
        elif self.shuffle:
            self._insert_upcoming(added, 1)
        self._schedule_queue_refresh()

    def action_cursor_down(self) -> None:
        focused = self.focused
        if isinstance(focused, ListView):
            focused.action_cursor_down()

    def action_cursor_up(self) -> None:
        focused = self.focused
        if isinstance(focused, ListView):
            focused.action_cursor_up()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search-input":
            return
        event.stop()
        self._start_search(event.value)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not isinstance(event.item, TrackItem):
            return
        if event.list_view.id == "results":
            self.play_at(self.results, event.index)
        elif event.list_view.id == "queue-list":
            self.play_at(list(self.queue), event.index)

    def on_seek_bar_sought(self, event: SeekBar.Sought) -> None:
        event.stop()
        if self.player is not None:
            self.player.seek_ratio(event.ratio)

    def on_volume_bar_changed(self, event: VolumeBar.Changed) -> None:
        event.stop()
        self.muted = event.level == 0
        self._set_volume(event.level)

    def play_at(self, tracks: list[Track], index: int) -> None:
        if not tracks or index < 0 or index >= len(tracks):
            return
        self.queue = list(tracks)
        self.queue_index = index
        if self.shuffle:
            self._build_order(index)
        else:
            self._order = []
        self._begin_playback(self.queue[index])
        self._schedule_queue_refresh()

    def _boot_player(self) -> None:
        if self.player is None:
            try:
                self.player = MpvPlayer(on_event=self._from_mpv, volume=self.volume)
                self.player.start()
            except Exception as exc:
                message = short_error(exc)
                self.player = DeadPlayer(message)
                self._set_status(message, error=True)
                return
        else:
            self.player.start()
        self.player.set_volume(0 if self.muted else self.volume)

    def _shutdown_player(self) -> None:
        player = self.player
        if player is not None:
            player.shutdown()

    def _from_mpv(self, event: dict[str, object]) -> None:
        try:
            self.call_from_thread(self._on_mpv, event)
        except Exception:
            return

    def _on_mpv(self, event: dict[str, object]) -> None:
        name = event.get("event")
        if name == "start-file":
            # Only this file's end may move the queue. The one we replaced must not.
            self._entry_id = event.get("playlist_entry_id")
            return
        if name == "playback-restart":
            self.loading = False
            self._skip_burst = 0
            self._paint_identity()
            return
        if name != "end-file":
            return
        reason = str(event.get("reason") or "")
        if reason in {"stop", "redirect", "quit"}:
            return
        if not self._same_entry(event):
            return
        self._entry_id = None
        if reason == "eof":
            if self.loop == "one":
                self._replay_current()
                return
            self._advance(1, auto=True)
            return
        if reason == "error":
            self.loading = False
            detail = str(event.get("file_error") or "")
            if not detail and self.player is not None:
                detail = self.player.log_tail()
            self._fail_forward(detail or "Playback failed")

    def _same_entry(self, event: dict[str, object]) -> bool:
        if self._entry_id is None:
            return False
        incoming = event.get("playlist_entry_id")
        if incoming is None:
            return True
        return incoming == self._entry_id

    def _fail_forward(self, message: str) -> None:
        """A dead stream should not stop the rest of the queue."""

        self._skip_burst += 1
        if self._skip_burst > SKIP_LIMIT or not self._has_next():
            self._set_status(message or "Playback failed", error=True)
            self._paint_identity()
            self.notify(message or "Playback failed")
            return
        self._advance(1, auto=True)

    def _start_search(self, query: str) -> None:
        query = " ".join(query.split())
        if not query:
            return
        self._search_generation += 1
        generation = self._search_generation
        kind = classify_query(query)
        if kind == "search":
            self._set_status("Searching…")
            work = partial(self._search_worker, query, generation)
            name = "search"
        else:
            self._set_status("Opening link…")
            work = partial(self._link_worker, query, generation, kind)
            name = "link"
        self.run_worker(
            work,
            thread=True,
            exclusive=False,
            group="search",
            exit_on_error=False,
            name=name,
        )

    def _search_worker(self, query: str, generation: int) -> None:
        try:
            tracks = self.source.search(query)
        except Exception as exc:
            self._deliver(self._search_failed, generation, exc)
            return
        self._deliver(self._search_finished, generation, tracks)

    def _search_failed(self, generation: int, exc: BaseException) -> None:
        if generation != self._search_generation:
            return
        self._set_status(short_error(exc), error=True)

    def _search_finished(self, generation: int, tracks: list[Track]) -> None:
        if generation != self._search_generation:
            return
        self.results = tracks
        if tracks:
            self._set_status(f"{len(tracks)} results  ·  streamed, nothing saved")
        else:
            self._set_status("No matches")
        self.call_later(self._show_search_results)

    def _link_worker(self, query: str, generation: int, kind: str) -> None:
        try:
            title, tracks, total = self.source.collect(query, kind)
        except Exception as exc:
            self._deliver(self._search_failed, generation, exc)
            return
        self._deliver(self._link_finished, generation, (title, tracks, total, kind))

    def _link_finished(
        self,
        generation: int,
        payload: tuple[str, list[Track], int | None, str],
    ) -> None:
        if generation != self._search_generation:
            return
        title, tracks, total, kind = payload
        self._queue_tracks(title, tracks, total, kind)

    def _queue_tracks(
        self,
        title: str,
        tracks: list[Track],
        total: int | None,
        kind: str,
    ) -> None:
        """Append a video or playlist. Start it when nothing is already playing."""

        if not tracks:
            self._set_status("That link has no playable songs", error=True)
            return
        start = len(self.queue)
        self.queue.extend(tracks)
        count = len(tracks)
        if kind == "playlist":
            if total and total > count:
                note = f"Queued {count} of {total} from {title}"
            else:
                note = f"Queued {count} from {title}"
        else:
            note = f"Queued {tracks[0].title}"
        if tracks[0].query and not tracks[0].video_id:
            note += "  ·  streaming from YouTube"
        if not self._playback_active():
            pick = start
            if self.shuffle and count > 1:
                pick = random.randrange(start, start + count)
            if self.shuffle:
                self._build_order(pick)
            self.queue_index = pick
            self._begin_playback(self.queue[pick])
            note = "Playing · " + note
        elif self.shuffle:
            self._insert_upcoming(start, count)
        self._set_status(note)
        self.notify(note)
        try:
            self.query_one("#search-input", Input).value = ""
        except NoMatches:
            pass
        self._schedule_queue_refresh()
        self._show_view("queue")

    def _playback_active(self) -> bool:
        if self.current is None or self.queue_index is None:
            return False
        if self.loading:
            return True
        if self.player is None:
            return False
        return not self.player.snapshot().idle

    async def _show_search_results(self) -> None:
        generation = self._search_generation
        await self._mount_tracks("#results", self.results)
        if generation != self._search_generation:
            return
        if self.results:
            results = self.query_one("#results", ListView)
            results.index = 0
            results.focus()

    def _begin_playback(self, track: Track) -> None:
        self.current = track
        self._entry_id = None
        self._play_generation += 1
        generation = self._play_generation
        self._set_status("")
        if isinstance(self.player, DeadPlayer):
            self.loading = False
            self._set_status(self.player.message, error=True)
            try:
                self._paint_identity()
                self._mark_playing()
            except NoMatches:
                return
            return
        if self.player is not None:
            self.player.stop()
        self.loading = True
        # Start the stream before painting. A row that is still mounting must not cancel playback.
        # Not exclusive: cancelling a thread worker can drop the resolve, so Next never starts.
        self.run_worker(
            partial(self._resolve_worker, track, generation),
            thread=True,
            exclusive=False,
            group="play",
            exit_on_error=False,
            name="play",
        )
        try:
            self._paint_identity()
            self._mark_playing()
        except NoMatches:
            return

    def _resolve_worker(self, track: Track, generation: int) -> None:
        try:
            stream = self.source.resolve(track)
        except Exception as exc:
            self._deliver(self._resolve_failed, generation, exc)
            return
        self._deliver(self._resolve_ready, generation, stream)

    def _resolve_failed(self, generation: int, exc: BaseException) -> None:
        if generation != self._play_generation:
            return
        self.loading = False
        self._fail_forward(short_error(exc))

    def _resolve_ready(self, generation: int, stream: Stream) -> None:
        if generation != self._play_generation or self.player is None:
            return
        try:
            self.player.load(stream)
        except Exception as exc:
            self.loading = False
            self._set_status(short_error(exc), error=True)
            self._paint_identity()

    def _build_order(self, start: int | None) -> None:
        """Current song first, then the rest of the queue in random order."""

        rest = [index for index in range(len(self.queue)) if index != start]
        random.shuffle(rest)
        if start is None or not 0 <= start < len(self.queue):
            self._order = rest
            return
        self._order = [start, *rest]

    def _insert_upcoming(self, start: int, count: int) -> None:
        """Drop newly queued songs into the remaining shuffle, after the current one."""

        if count <= 0:
            return
        new = list(range(start, start + count))
        random.shuffle(new)
        if self.queue_index in self._order:
            at = self._order.index(self.queue_index)
            head = self._order[: at + 1]
            tail = [index for index in self._order[at + 1 :] if index < start]
        else:
            head = [index for index in self._order if index < start]
            tail = []
        for index in new:
            tail.insert(random.randint(0, len(tail)), index)
        self._order = head + tail

    def _ensure_order(self) -> None:
        expected = list(range(len(self.queue)))
        if sorted(self._order) != expected:
            current = self.queue_index if self.queue_index in expected else None
            self._build_order(current)

    def _neighbor(self, step: int) -> int:
        """Queue index `step` songs away. Out of range means the end or the start."""

        if self.queue_index is None:
            return -1
        if not self.shuffle:
            return self.queue_index + step
        self._ensure_order()
        if self.queue_index in self._order:
            at = self._order.index(self.queue_index)
        else:
            at = -1
        target = at + step
        if target < 0:
            return -1
        if target >= len(self._order):
            return len(self.queue)
        return self._order[target]

    def _has_next(self) -> bool:
        if self.queue_index is None or not self.queue:
            return False
        if self.loop == "all":
            return True
        nxt = self._neighbor(1)
        return 0 <= nxt < len(self.queue)

    def _wrap_index(self, step: int) -> int:
        """First song after the queue ends, or the last song before it starts."""

        if self.shuffle:
            self._ensure_order()
            if step > 0:
                self._build_order(None)
                return self._order[0] if self._order else 0
            return self._order[-1] if self._order else len(self.queue) - 1
        if step > 0:
            return 0
        return len(self.queue) - 1

    def _replay_current(self) -> None:
        if self.queue_index is None or not self.queue:
            return
        self._begin_playback(self.queue[self.queue_index])
        self._schedule_queue_refresh()

    def _advance(self, step: int, auto: bool) -> None:
        if self.queue_index is None or not self.queue:
            return
        nxt = self._neighbor(step)
        if nxt < 0 or nxt >= len(self.queue):
            if self.loop == "all":
                nxt = self._wrap_index(step)
            elif nxt < 0:
                if self.player is not None:
                    self.player.seek_ratio(0)
                return
            else:
                self.loading = False
                self._set_status("End of queue")
                self._paint_identity()
                if not auto:
                    self.notify("End of queue")
                return
        self.queue_index = nxt
        self._begin_playback(self.queue[nxt])
        self._schedule_queue_refresh()

    def _deliver(self, callback: object, generation: int, payload: object) -> None:
        try:
            self.call_from_thread(callback, generation, payload)  # type: ignore[arg-type]
        except Exception:
            return

    def _tick(self) -> None:
        if self.player is None:
            return
        try:
            snapshot = self.player.snapshot()
            if (
                self.loading
                and snapshot.position
                and snapshot.position > 0.15
                and not snapshot.idle
            ):
                self.loading = False
                self._paint_identity()
            self._paint_progress(snapshot)
            self._paint_play_button(snapshot)
            self._paint_skip_buttons(snapshot)
        except NoMatches:
            return

    def _paint_identity(self) -> None:
        title = self.query_one("#track-title", Static)
        artist = self.query_one("#track-artist", Static)
        big = self.query_one("#big-art", Static)
        now_title = self.query_one("#now-title", Static)
        now_artist = self.query_one("#now-artist", Static)
        if self.current is None:
            title.update("Nothing playing")
            artist.update("Search for a song to start")
            self._paint_cover(big, "♫", "#282828", "#ffffff")
            now_title.update("Nothing playing")
            now_artist.update("Search for a song and press enter")
            self.query_one("#up-next", Static).update("Nothing queued yet")
            return
        track = self.current
        color = art_color(track.video_id or f"{track.artist} {track.title}")
        letter = track.title[:1].upper() or "♫"
        title.update(track.title)
        if self.loading:
            artist.update("Opening stream…")
        else:
            position = ""
            if self.queue_index is not None and self.queue:
                position = f"   ·   {self.queue_index + 1} of {len(self.queue)}"
            artist.update(f"{track.artist}{position}")
        self._paint_cover(big, letter, color, "#ffffff")
        now_title.update(track.title)
        now_artist.update(track.artist)
        self._paint_up_next()

    def _paint_cover(self, widget: Static, letter: str, background: str, color: str) -> None:
        widget.update(letter)
        widget.styles.background = background
        widget.styles.color = color

    def _paint_up_next(self) -> None:
        label = self.query_one("#up-next", Static)
        if self.queue_index is None:
            label.update("")
            return
        if self.shuffle:
            self._ensure_order()
            if self.queue_index in self._order:
                at = self._order.index(self.queue_index)
            else:
                at = -1
            indexes = self._order[at + 1 : at + 4]
        else:
            indexes = list(range(self.queue_index + 1, min(len(self.queue), self.queue_index + 4)))
        if not indexes:
            label.update("Nothing else in the queue")
            return
        lines = [f"{index + 1:>2}   {self.queue[index].title}" for index in indexes]
        label.update("\n".join(lines))

    def _paint_progress(self, snapshot: PlaybackSnapshot) -> None:
        position = snapshot.position or 0.0
        duration = snapshot.duration
        active = self.current is not None and duration is not None and duration > 0
        fraction = 0.0
        if active and duration:
            fraction = min(1.0, max(0.0, position / duration))
        bar = self.query_one("#seek-bar", SeekBar)
        bar.active = active
        bar.fraction = fraction
        self.query_one("#time-pos", Static).update(format_duration(position if active else 0))
        self.query_one("#time-dur", Static).update(
            format_duration(duration) if active else "--:--"
        )

    def _paint_play_button(self, snapshot: PlayerSnapshot) -> None:
        playing = (
            self.current is not None
            and not self.loading
            and not snapshot.paused
            and not snapshot.idle
        )
        label = "❚❚" if playing else "▶"
        if label == self._play_label:
            return
        self._play_label = label
        self.query_one("#btn-play", Static).update(label)

    def _paint_shuffle(self) -> None:
        self.query_one("#btn-shuffle").set_class(self.shuffle, "on")

    def _paint_loop(self) -> None:
        button = self.query_one("#btn-loop", Static)
        button.set_class(self.loop != "off", "on")
        label = "1" if self.loop == "one" else "↻"
        if label != self._loop_label:
            self._loop_label = label
            button.update(label)

    def _paint_skip_buttons(self, snapshot: PlaybackSnapshot) -> None:
        has_prev = self.loop == "all" or self._neighbor(-1) >= 0 or bool(
            snapshot.position and snapshot.position > 3 and self.current is not None
        )
        self.query_one("#btn-prev").set_class(not has_prev, "dim")
        self.query_one("#btn-next").set_class(not self._has_next(), "dim")
        self._paint_shuffle()
        self._paint_loop()

    def _paint_volume(self) -> None:
        bar = self.query_one("#volume-bar", VolumeBar)
        bar.muted = self.muted
        bar.level = self.volume
        readout = "off" if self.muted else f"{self.volume:>3}"
        self.query_one("#volume-readout", Static).update(readout)

    def _set_volume(self, value: int) -> None:
        self.volume = max(0, min(100, int(value)))
        if self.volume > 0:
            self.muted = False
        self._apply_volume()
        self._paint_volume()

    def _apply_volume(self) -> None:
        save_volume(self.config_path, self.volume)
        if self.player is not None:
            self.player.set_volume(0 if self.muted else self.volume)

    def _seek(self, seconds: float) -> None:
        if self.player is None or self.current is None:
            return
        self.player.seek_relative(seconds)

    def _show_view(self, name: str) -> None:
        for view_name in ("search", "queue", "now"):
            shown = view_name == name
            self.query_one(f"#view-{view_name}").set_class(shown, "visible")
            self.query_one(f"#nav-{view_name}").set_class(shown, "active")
        self._focus_view(name)

    def _focus_view(self, name: str) -> None:
        """Move focus onto the visible view so keys are not stuck in a hidden list."""

        if name == "search" and self.results:
            self.query_one("#results", ListView).focus()
            return
        if name == "search":
            self.query_one("#search-input", Input).focus()
            return
        if name == "queue" and self.queue:
            self.query_one("#queue-list", ListView).focus()
            return
        self.screen.set_focus(None)

    def _set_status(self, text: str, error: bool = False) -> None:
        try:
            status = self.query_one("#status", Static)
        except NoMatches:
            return
        status.update(text)
        status.set_class(error, "error")

    def _highlighted_track(self) -> Track | None:
        focused = self.focused
        if not isinstance(focused, ListView):
            return None
        item = focused.highlighted_child
        if isinstance(item, TrackItem):
            return item.track
        return None

    def _mark_playing(self) -> None:
        current_id = self.current.video_id if self.current else None
        for item in self.query(TrackItem):
            item.set_playing(item.track.video_id == current_id)

    def _update_empty_states(self) -> None:
        self.query_one("#results").set_class(bool(self.results), "show")
        self.query_one("#empty-search").set_class(not self.results, "show")
        self.query_one("#queue-list").set_class(bool(self.queue), "show")
        self.query_one("#empty-queue").set_class(not self.queue, "show")

    def _schedule_queue_refresh(self) -> None:
        self.call_later(self._mount_queue_list)

    async def _mount_queue_list(self) -> None:
        await self._mount_tracks("#queue-list", self.queue)
        if not self.queue or self.queue_index is None:
            return
        queue = self.query_one("#queue-list", ListView)
        queue.index = min(self.queue_index, len(self.queue) - 1)

    async def _mount_tracks(self, list_id: str, tracks: list[Track]) -> None:
        async with self._ui_lock:
            view = self.query_one(list_id, ListView)
            await view.clear()
            current_id = self.current.video_id if self.current else None
            items = [
                TrackItem(track, index + 1, playing=track.video_id == current_id)
                for index, track in enumerate(tracks)
            ]
            if items:
                await view.extend(items)
            self._update_empty_states()
