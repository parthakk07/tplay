"""mpv controller. Audio is streamed; the cache is not kept on disk."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from tplay.models import Stream


class PlayerError(Exception):
    """mpv could not be started or controlled."""


@dataclass
class PlaybackSnapshot:
    """Latest playback numbers reported by mpv."""

    position: float | None = None
    duration: float | None = None
    paused: bool = True
    volume: float = 80.0
    idle: bool = True


class DeadPlayer:
    """Stand-in used when mpv is missing so the rest of the UI still runs."""

    def __init__(self, message: str) -> None:
        self.message = message
        self._snapshot = PlaybackSnapshot()

    def start(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def load(self, stream: Stream) -> None:
        raise PlayerError(self.message)

    def stop(self) -> None:
        return None

    def toggle_pause(self) -> None:
        return None

    def set_volume(self, volume: int) -> None:
        return None

    def seek_relative(self, seconds: float) -> None:
        return None

    def seek_ratio(self, ratio: float) -> None:
        return None

    def snapshot(self) -> PlaybackSnapshot:
        return self._snapshot

    def log_tail(self) -> str:
        return self.message


class MpvPlayer:
    """One long-lived mpv process controlled over its JSON IPC socket."""

    def __init__(
        self,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        volume: int = 80,
        audio_output: str | None = None,
    ) -> None:
        self._on_event = on_event
        self._volume = max(0, min(100, int(volume)))
        self._audio_output = audio_output
        self._proc: subprocess.Popen[bytes] | None = None
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._snapshot = PlaybackSnapshot(volume=float(self._volume))
        self._live = False
        self._closed = False
        self._started = False
        pid = os.getpid()
        self.socket_path = Path(tempfile.gettempdir()) / f"tplay-{pid}.sock"
        self._log_path = Path(tempfile.gettempdir()) / f"tplay-{pid}.log"
        self._log_file: Any = None

    def start(self) -> None:
        if self._started:
            return
        if shutil.which("mpv") is None:
            raise PlayerError("mpv is not installed. Install it with: sudo apt install mpv")
        self.socket_path.unlink(missing_ok=True)
        self._log_file = open(self._log_path, "w", encoding="utf-8")
        command = [
            "mpv",
            "--idle=yes",
            "--no-video",
            "--no-terminal",
            "--force-window=no",
            "--input-default-bindings=no",
            f"--input-ipc-server={self.socket_path}",
            f"--volume={self._volume}",
            "--volume-max=100",
            "--ytdl=no",
            "--cache-on-disk=no",
            "--audio-client-name=tplay",
            "--gapless-audio=weak",
            "--keep-open=no",
            "--msg-level=all=warn",
        ]
        if self._audio_output:
            command.append(f"--ao={self._audio_output}")
        try:
            self._proc = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=self._log_file,
                start_new_session=True,
            )
        except OSError as exc:
            self.shutdown()
            raise PlayerError(f"Could not start mpv: {exc}") from exc
        try:
            self._connect()
            self._thread = threading.Thread(
                target=self._read_loop, name="tplay-mpv", daemon=True
            )
            self._thread.start()
            self.set_volume(self._volume)
            self._observe()
            self._started = True
        except Exception:
            detail = self.log_tail()
            self.shutdown()
            message = "mpv did not start"
            if detail:
                message = f"{message}: {detail}"
            raise PlayerError(message)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._started = False
        self._send(["quit"])
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._kill_proc()
        self._close_sock()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._close_log()
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            pass

    def stop(self) -> None:
        """Stop the current file so a skip cannot be confused with it ending."""

        with self._state_lock:
            self._live = False
            self._snapshot.idle = True
            self._snapshot.position = 0.0
        self._send(["stop"])

    def load(self, stream: Stream) -> None:
        """Play a direct URL. mpv reads it as a stream; disk cache stays off."""

        user_agent, header_lines = _header_lines(stream.headers)
        with self._state_lock:
            # Ignore time updates until start-file. Buffered events belong to the old file.
            self._live = False
            self._snapshot.position = 0.0
            self._snapshot.duration = None
            self._snapshot.paused = False
            self._snapshot.idle = True
        if user_agent:
            self._send(["set_property", "user-agent", user_agent])
        self._send(["set_property", "http-header-fields", header_lines])
        self._send(["loadfile", stream.url, "replace"])

    def toggle_pause(self) -> None:
        self._send(["cycle", "pause"])

    def set_volume(self, volume: int) -> None:
        self._volume = max(0, min(100, int(volume)))
        self._send(["set_property", "volume", self._volume])
        with self._state_lock:
            self._snapshot.volume = float(self._volume)

    def seek_relative(self, seconds: float) -> None:
        self._send(["seek", float(seconds), "relative"])

    def seek_ratio(self, ratio: float) -> None:
        snapshot = self.snapshot()
        if not snapshot.duration or snapshot.duration <= 0:
            return
        bounded = min(1.0, max(0.0, ratio))
        target = min(snapshot.duration - 0.25, max(0.0, bounded * snapshot.duration))
        self._send(["seek", target, "absolute"])

    def snapshot(self) -> PlaybackSnapshot:
        with self._state_lock:
            return replace(self._snapshot)

    def log_tail(self) -> str:
        try:
            text = self._log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        return lines[-1][:240]

    def _connect(self) -> None:
        deadline = _now() + 3.0
        last_error = ""
        while _now() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                break
            if self.socket_path.exists():
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    sock.connect(str(self.socket_path))
                except OSError as exc:
                    last_error = str(exc)
                    sock.close()
                else:
                    self._sock = sock
                    return
            _sleep(0.05)
        detail = last_error or self.log_tail() or "timed out waiting for mpv"
        raise PlayerError(detail)

    def _observe(self) -> None:
        properties = ("time-pos", "duration", "pause", "volume", "idle-active")
        for index, name in enumerate(properties, start=1):
            self._send(["observe_property", index, name])

    def _read_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        buffer = b""
        while not self._closed:
            try:
                chunk = sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                self._handle_line(line)

    def _handle_line(self, raw: bytes) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(message, dict):
            return
        event_name = message.get("event")
        if event_name == "start-file":
            with self._state_lock:
                self._live = True
                self._snapshot.position = 0.0
                self._snapshot.duration = None
                self._snapshot.paused = False
                self._snapshot.idle = False
        if event_name == "property-change":
            name = str(message.get("name") or "")
            # Volume still applies while idle. Position events from the previous file do not.
            if name != "volume":
                with self._state_lock:
                    live = self._live
                if not live:
                    return
            self._apply_property(name, message.get("data"))
            return
        if event_name and self._on_event is not None:
            try:
                self._on_event(message)
            except Exception:
                return

    def _apply_property(self, name: str, data: object) -> None:
        with self._state_lock:
            if name == "time-pos":
                self._snapshot.position = float(data) if _is_number(data) else None
            elif name == "duration":
                self._snapshot.duration = float(data) if _is_number(data) else None
            elif name == "pause":
                self._snapshot.paused = bool(data) if data is not None else True
            elif name == "volume" and _is_number(data):
                self._snapshot.volume = float(data)
            elif name == "idle-active":
                self._snapshot.idle = bool(data)

    def _send(self, command: list[object]) -> None:
        sock = self._sock
        if sock is None:
            return
        payload = json.dumps({"command": command}, ensure_ascii=False) + "\n"
        with self._write_lock:
            try:
                sock.sendall(payload.encode("utf-8"))
            except OSError:
                return

    def _kill_proc(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, 15)
        except OSError:
            proc.terminate()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                proc.kill()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                return

    def _close_sock(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _close_log(self) -> None:
        log_file = self._log_file
        self._log_file = None
        if log_file is None:
            return
        try:
            log_file.close()
        except OSError:
            pass


def _header_lines(headers: dict[str, str]) -> tuple[str | None, list[str]]:
    user_agent = None
    lines: list[str] = []
    for key, value in headers.items():
        clean = " ".join(str(value).split())
        if not clean:
            continue
        if key.lower() == "user-agent":
            user_agent = clean
        else:
            lines.append(f"{key}: {clean}")
    return user_agent, lines


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _now() -> float:
    import time

    return time.monotonic()


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
