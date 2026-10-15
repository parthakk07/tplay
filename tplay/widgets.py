"""Spotify-style terminal widgets: sidebar, lists, transport, and help."""

from __future__ import annotations

from math import ceil

from rich.color import Color
from rich.segment import Segment, Segments
from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult, RenderResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.events import Click, Key, MouseScrollDown, MouseScrollUp
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.scrollbar import ScrollBar, ScrollBarRender
from textual.widget import Widget
from textual.widgets import Input, ListItem, ListView, Static

from tplay.models import Track

# Short enough that holding j/k stays with the highlight, long enough to glide.
_SCROLL_GLIDE = 0.12


class ThinScrollBarRender(ScrollBarRender):
    """One hairline thumb. The library bar is a reversed cell plus stepped caps."""

    @classmethod
    def render_bar(
        cls,
        size: int = 25,
        virtual_size: float = 50,
        window_size: float = 20,
        position: float = 0,
        thickness: int = 1,
        vertical: bool = True,
        back_color: Color = Color.parse("#555555"),
        bar_color: Color = Color.parse("bright_magenta"),
    ) -> Segments:
        width = thickness if vertical else 1
        blank = " " * width
        # A box-drawing stroke, the same on every row, so the thumb is one line.
        stroke = ("│" if vertical else "─") * width
        size_i = max(0, int(size))
        if (
            size_i
            and window_size
            and virtual_size
            and size != virtual_size
            and virtual_size > window_size
        ):
            thumb_cells = max(1, min(size_i, ceil(window_size * size_i / virtual_size)))
            room = virtual_size - window_size
            start = int((size_i - thumb_cells) * (position / room))
            start = max(0, min(start, size_i - thumb_cells))
            end = start + thumb_cells
            upper = {"@mouse.down": "scroll_up"}
            lower = {"@mouse.down": "scroll_down"}
            grab = {"@mouse.down": "grab"}
            above = Segment(blank, Style(bgcolor=back_color, meta=upper))
            below = Segment(blank, Style(bgcolor=back_color, meta=lower))
            thumb = Segment(
                stroke,
                Style(color=bar_color, bgcolor=back_color, meta=grab),
            )
            segments = [above] * start + [thumb] * (end - start) + [below] * (size_i - end)
        else:
            segments = [Segment(blank, Style(bgcolor=back_color))] * size_i
        if vertical:
            return Segments(segments, new_lines=True)
        return Segments((segments + [Segment.line()]) * thickness, new_lines=False)


ScrollBar.renderer = ThinScrollBarRender


def _glide_scroll(widget: Widget, event: MouseScrollDown | MouseScrollUp, *, down: bool) -> None:
    """Wheel movement eases to the next rows instead of jumping."""

    horizontal = bool(getattr(event, "ctrl", False) or getattr(event, "shift", False))
    if horizontal:
        if not widget.allow_horizontal_scroll:
            return
        moved = (
            widget._scroll_right_for_pointer(animate=True, duration=_SCROLL_GLIDE, easing="out_cubic")
            if down
            else widget._scroll_left_for_pointer(animate=True, duration=_SCROLL_GLIDE, easing="out_cubic")
        )
    else:
        if not widget.allow_vertical_scroll:
            return
        moved = (
            widget._scroll_down_for_pointer(animate=True, duration=_SCROLL_GLIDE, easing="out_cubic")
            if down
            else widget._scroll_up_for_pointer(animate=True, duration=_SCROLL_GLIDE, easing="out_cubic")
        )
    if moved:
        event.stop()


class _GlideScroll:
    """Mouse-wheel scrolling that glides. Keyboard lists override the highlight scroll."""

    def _on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        _glide_scroll(self, event, down=True)  # type: ignore[arg-type]

    def _on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        _glide_scroll(self, event, down=False)  # type: ignore[arg-type]


class SmoothList(_GlideScroll, ListView):
    """A list whose highlight glides into view."""

    def watch_index(self, old_index: int | None, new_index: int | None) -> None:
        if new_index is not None and 0 <= new_index < len(self._nodes):
            selected = self._nodes[new_index]
            if selected.region:
                self.scroll_to_widget(
                    selected,
                    animate=True,
                    duration=_SCROLL_GLIDE,
                    easing="out_cubic",
                )
            else:
                self.call_after_refresh(
                    self.scroll_to_widget,
                    selected,
                    animate=True,
                    duration=_SCROLL_GLIDE,
                    easing="out_cubic",
                )

        if self._is_valid_index(old_index):
            self._nodes[old_index].highlighted = False

        if (
            new_index is not None
            and self._is_valid_index(new_index)
            and not self._nodes[new_index].disabled
        ):
            child = self._nodes[new_index]
            child.highlighted = True
            self.post_message(self.Highlighted(self, child))
        else:
            self.post_message(self.Highlighted(self, None))


class SmoothPane(_GlideScroll, Vertical):
    """A vertical pane whose wheel scroll glides."""

EMPTY_SEARCH = (
    "Type a song or artist and press enter.\n"
    "Paste a YouTube or Spotify playlist link to queue it.\n"
    "\n"
    "Audio streams from YouTube. Nothing is saved.\n"
    "A Spotify playlist plays those songs from YouTube.\n"
    "\n"
    "/  search        enter  play        a  queue\n"
    "space  pause     + -  volume       ?  shortcuts"
)

HELP_TEXT = """\
Keyboard                                          esc closes

  /              focus search
  enter          play the highlighted song
  paste a link   queue a YouTube or Spotify playlist
  a              add the highlighted song to the queue
  space          play or pause
  n / p          next / previous
  s              shuffle
  left / right   seek 10 seconds
  shift+arrows   seek 30 seconds
  + / -          volume
  m              mute
  1 / 2 / 3      search / queue / now playing
  j / k          move through the list
  ?              this help
  ctrl+c         quit

Click a row to play it.
Click the progress bar to seek.
Scroll or click the volume bar to change volume.

Audio streams directly. Nothing is written to disk.
"""


class ActionLabel(Static):
    """A borderless click target that runs an app action."""

    def __init__(self, label: str, action: str, **kwargs: object) -> None:
        super().__init__(label, markup=False, **kwargs)  # type: ignore[arg-type]
        self.action_name = action

    def on_click(self, event: Click) -> None:
        event.stop()
        callback = getattr(self.app, f"action_{self.action_name}", None)
        if callback is not None:
            callback()


class TrackItem(ListItem):
    """One row in the search results or the queue."""

    def __init__(self, track: Track, number: int, playing: bool = False) -> None:
        super().__init__(classes="playing" if playing else "")
        self.track = track
        self.number = number
        self._playing = playing

    def compose(self) -> ComposeResult:
        mark = "▶" if self._playing else f"{self.number:>2}"
        yield Static(mark, classes="marker", markup=False)
        with Vertical(classes="track-text"):
            yield Static(self.track.title, classes="title", markup=False)
            yield Static(self.track.artist, classes="artist", markup=False)
        yield Static(self.track.duration_text, classes="length", markup=False)

    def set_playing(self, playing: bool) -> None:
        changed = playing != self._playing
        self._playing = playing
        self.set_class(playing, "playing")
        if not changed:
            return
        # The row can be asked to repaint while its children are still mounting.
        try:
            marker = self.query_one(".marker", Static)
        except NoMatches:
            return
        marker.update("▶" if playing else f"{self.number:>2}")


class SeekBar(Static):
    """Clickable progress bar. Played audio is green, the rest is gray."""

    fraction = reactive(0.0)
    active = reactive(False)

    class Sought(Message):
        def __init__(self, ratio: float) -> None:
            super().__init__()
            self.ratio = ratio

    def render(self) -> RenderResult:
        width = max(1, self.size.width)
        if not self.active:
            return Text("━" * width, style="#3e3e3e")
        pos = round(min(1.0, max(0.0, self.fraction)) * (width - 1))
        pos = min(width - 1, max(0, pos))
        text = Text()
        text.append("━" * pos, style="#1db954")
        text.append("●", style="#ffffff")
        text.append("━" * (width - pos - 1), style="#535353")
        return text

    def on_click(self, event: Click) -> None:
        width = max(1, self.size.width)
        offset = event.get_content_offset(self)
        cell = offset.x if offset is not None else event.x
        ratio = min(1.0, max(0.0, cell / width))
        self.post_message(self.Sought(ratio))


class VolumeBar(Static):
    """Clickable volume meter."""

    level = reactive(80)
    muted = reactive(False)

    class Changed(Message):
        def __init__(self, level: int) -> None:
            super().__init__()
            self.level = level

    def render(self) -> RenderResult:
        width = max(1, self.size.width)
        amount = 0 if self.muted else max(0, min(100, int(self.level)))
        filled = round(amount / 100 * width)
        filled = min(width, max(0, filled))
        color = "#4d4d4d" if self.muted else "#1db954"
        text = Text()
        text.append("█" * filled, style=color)
        text.append("░" * (width - filled), style="#3e3e3e")
        return text

    def on_click(self, event: Click) -> None:
        width = max(1, self.size.width)
        offset = event.get_content_offset(self)
        cell = offset.x if offset is not None else event.x
        level = round(min(1.0, max(0.0, cell / max(1, width - 1))) * 100)
        self.post_message(self.Changed(max(0, min(100, level))))


class VolumeBox(Horizontal):
    """Volume controls. The mouse wheel adjusts the level."""

    def compose(self) -> ComposeResult:
        yield ActionLabel("−", "volume_down", id="btn-vol-down", classes="vol-btn")
        yield VolumeBar(id="volume-bar")
        yield ActionLabel("+", "volume_up", id="btn-vol-up", classes="vol-btn")
        yield Static(" 80", id="volume-readout", markup=False)

    def on_mouse_scroll_up(self, event: object) -> None:
        self.app.action_volume_up()

    def on_mouse_scroll_down(self, event: object) -> None:
        self.app.action_volume_down()


class Sidebar(Vertical):
    def __init__(self) -> None:
        super().__init__(id="sidebar")

    def compose(self) -> ComposeResult:
        yield Static("[#1db954]♫[/]  [#ffffff]tplay[/]", id="logo")
        yield Static("stream only  ·  no files", id="tagline", markup=False)
        yield ActionLabel("1   ⌕   Search", "show_search", id="nav-search", classes="nav active")
        yield ActionLabel("2   ≡   Queue", "show_queue", id="nav-queue", classes="nav")
        yield ActionLabel("3   ♪   Now playing", "show_now", id="nav-now", classes="nav")
        yield Static("", id="sidebar-gap")
        yield Static(
            "space  play / pause\n+ -    volume\n/      search\n?      shortcuts",
            id="sidebar-keys",
            markup=False,
        )


class SearchView(Vertical):
    def __init__(self) -> None:
        super().__init__(id="view-search", classes="view visible")

    def compose(self) -> ComposeResult:
        yield Static("Search", classes="page-title", markup=False)
        yield Input(
            placeholder="Song, artist, or a YouTube or Spotify playlist link",
            id="search-input",
            select_on_focus=False,
        )
        yield Static("", id="status", markup=False)
        yield SmoothList(id="results")
        yield Static(EMPTY_SEARCH, id="empty-search", classes="show", markup=False)


class QueueView(Vertical):
    def __init__(self) -> None:
        super().__init__(id="view-queue", classes="view")

    def compose(self) -> ComposeResult:
        yield Static("Queue", classes="page-title", markup=False)
        yield Static("Paste a YouTube or Spotify playlist link in Search.", id="queue-hint", markup=False)
        yield SmoothList(id="queue-list")
        yield Static(
            "The queue is empty.\n\nPlay a search result, press a on a song,\nor paste a YouTube or Spotify playlist link in Search.",
            id="empty-queue",
            classes="show",
            markup=False,
        )


class NowView(Vertical):
    def __init__(self) -> None:
        super().__init__(id="view-now", classes="view")

    def compose(self) -> ComposeResult:
        yield Static("Now playing", classes="page-title", markup=False)
        with SmoothPane(id="now-body"):
            with Horizontal(id="art-row"):
                yield Static("♫", id="big-art", markup=False)
            yield Static("Nothing playing", id="now-title", markup=False)
            yield Static("Search for a song and press enter", id="now-artist", markup=False)
            yield Static(
                "Streaming from YouTube  ·  nothing is saved",
                id="now-note",
                markup=False,
            )
            yield Static("Up next", id="up-next-label", markup=False)
            yield Static("", id="up-next", markup=False)


class PlayerBar(Vertical):
    def __init__(self) -> None:
        super().__init__(id="player")

    def compose(self) -> ComposeResult:
        with Horizontal(id="player-top"):
            yield Static("♫", id="art", markup=False)
            with Vertical(id="player-meta"):
                yield Static("Nothing playing", id="track-title", markup=False)
                yield Static("Search for a song to start", id="track-artist", markup=False)
            with Horizontal(id="transport"):
                yield ActionLabel("⇄", "toggle_shuffle", id="btn-shuffle", classes="transport-btn")
                yield ActionLabel("⏮", "previous_track", id="btn-prev", classes="transport-btn")
                yield ActionLabel("▶", "toggle_pause", id="btn-play", classes="transport-btn play")
                yield ActionLabel("⏭", "next_track", id="btn-next", classes="transport-btn")
            yield VolumeBox(id="volume-box")
        with Horizontal(id="player-bottom"):
            yield Static("0:00", id="time-pos", markup=False)
            yield SeekBar(id="seek-bar")
            yield Static("--:--", id="time-dur", markup=False)


class HelpScreen(ModalScreen[None]):
    """Shortcut card. Any key or click closes it."""

    def compose(self) -> ComposeResult:
        yield Static(HELP_TEXT, id="help-card", markup=False)

    def on_key(self, event: Key) -> None:
        event.stop()
        self.dismiss()

    def on_click(self, event: Click) -> None:
        self.dismiss()
