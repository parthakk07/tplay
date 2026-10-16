"""Icons for the player, with ASCII when a terminal font has no glyph.

A missing glyph is drawn as a box. The fallback is readable text and does
not change which key or click runs the control.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
from pathlib import Path

# "plain" forces ASCII, "fancy" keeps the symbols, None follows the terminal.
_forced: str | None = None
_coverage: set[int] | None = None
_coverage_loaded = False

_LIMITED_TERMS = {"dumb", "linux", "vt100", "vt102", "vt220", "ansi"}


def glyph(fancy: str, plain: str) -> str:
    """Return the symbol, or the ASCII text when this terminal cannot draw it."""

    if _forced == "plain" or (_forced != "fancy" and _plain_terminal()):
        return plain
    if not _can_encode(fancy):
        return plain
    if _forced == "fancy":
        return fancy
    covered = _font_coverage()
    if covered is None:
        return fancy
    if any(ord(char) not in covered for char in fancy):
        return plain
    return fancy


def _plain_terminal() -> bool:
    if os.environ.get("TPLAY_ASCII") == "1":
        return True
    if os.environ.get("TPLAY_GLYPHS") == "1":
        return False
    return os.environ.get("TERM", "") in _LIMITED_TERMS


def _can_encode(text: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (LookupError, UnicodeError):
        return False
    return True


def _font_coverage() -> set[int] | None:
    global _coverage, _coverage_loaded
    if _coverage_loaded:
        return _coverage
    _coverage_loaded = True
    _coverage = _load_font_coverage()
    return _coverage


def _load_font_coverage() -> set[int] | None:
    path = _monospace_font()
    if not path:
        return None
    return _cmap_codepoints(path)


def _monospace_font() -> str | None:
    try:
        out = subprocess.check_output(
            ["fc-match", "-f", "%{file}", "monospace"],
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    file = out.strip()
    if not file or not Path(file).is_file():
        return None
    return file


def _cmap_codepoints(path: str) -> set[int] | None:
    """Code points with a real glyph in a TrueType or OpenType font."""

    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if len(data) < 12 or data[:4] not in {b"\x00\x01\x00\x00", b"OTTO", b"true"}:
        return None
    try:
        cmap_off = _table_offset(data, b"cmap")
    except (struct.error, ValueError):
        return None
    if cmap_off is None or cmap_off + 4 > len(data):
        return None
    try:
        _version, count = struct.unpack(">HH", data[cmap_off : cmap_off + 4])
    except struct.error:
        return None
    found: set[int] = set()
    for index in range(count):
        start = cmap_off + 4 + index * 8
        if start + 8 > len(data):
            return None
        _platform, _encoding, suboff = struct.unpack(">HHI", data[start : start + 8])
        found.update(_cmap_subtable(data, cmap_off + suboff))
    return found


def _table_offset(data: bytes, tag: bytes) -> int | None:
    count = struct.unpack(">H", data[4:6])[0]
    for index in range(count):
        rec = 12 + index * 16
        if rec + 16 > len(data):
            return None
        name, _checksum, offset, _length = struct.unpack(">4sIII", data[rec : rec + 16])
        if name == tag:
            return offset
    return None


def _cmap_subtable(data: bytes, start: int) -> set[int]:
    if start + 2 > len(data):
        return set()
    fmt = struct.unpack(">H", data[start : start + 2])[0]
    if fmt == 4:
        return _cmap_format4(data, start)
    if fmt == 12:
        return _cmap_format12(data, start)
    return set()


def _cmap_format4(data: bytes, start: int) -> set[int]:
    if start + 14 > len(data):
        return set()
    _length, _lang, seg_x2 = struct.unpack(">HHH", data[start + 2 : start + 8])
    segments = seg_x2 // 2
    pos = start + 14
    need = pos + 2 + segments * 8
    if segments <= 0 or need > len(data):
        return set()
    ends = list(struct.unpack(f">{segments}H", data[pos : pos + segments * 2]))
    pos += segments * 2 + 2
    starts = list(struct.unpack(f">{segments}H", data[pos : pos + segments * 2]))
    pos += segments * 2
    deltas = list(struct.unpack(f">{segments}h", data[pos : pos + segments * 2]))
    pos += segments * 2
    offsets = list(struct.unpack(f">{segments}H", data[pos : pos + segments * 2]))
    id_pos = pos
    found: set[int] = set()
    for index, (start_c, end_c) in enumerate(zip(starts, ends)):
        if start_c == 0xFFFF:
            continue
        for code in range(start_c, end_c + 1):
            if offsets[index] == 0:
                glyph_id = (code + deltas[index]) & 0xFFFF
            else:
                byte = offsets[index] + 2 * (code - start_c)
                at = id_pos + index * 2 + byte
                if at + 2 > len(data):
                    continue
                glyph_id = struct.unpack(">H", data[at : at + 2])[0]
                if glyph_id:
                    glyph_id = (glyph_id + deltas[index]) & 0xFFFF
            if glyph_id:
                found.add(code)
    return found


def _cmap_format12(data: bytes, start: int) -> set[int]:
    if start + 16 > len(data):
        return set()
    _length, _lang, groups = struct.unpack(">III", data[start + 4 : start + 16])
    pos = start + 16
    found: set[int] = set()
    for _group in range(groups):
        if pos + 12 > len(data):
            break
        start_c, end_c, glyph_id = struct.unpack(">III", data[pos : pos + 12])
        pos += 12
        if glyph_id:
            found.update(range(start_c, end_c + 1))
    return found
