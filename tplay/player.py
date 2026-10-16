"""mpv controller. Audio is streamed; the cache is not kept on disk."""

from __future__ import annotations

import atexit
import ctypes
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from tplay.models import Stream


class PlayerError(Exception):
    """mpv could not be started or controlled."""


def mpv_install_hint() -> str:
    """Command that installs mpv on this operating system."""

    if sys.platform == "win32":
        return "winget install --exact --id shinchiro.mpv"
    if sys.platform == "darwin":
        return "brew install mpv"
    return "sudo apt install mpv"


def mpv_missing_message() -> str:
    """What to show when audio cannot start. This is not a crash."""

    return f"mpv is not installed, so audio cannot play. Install it with: {mpv_install_hint()}"


def _mpv_candidates() -> list[str]:
    """Places mpv is often installed when it was left off PATH."""

    home = Path.home()
    if sys.platform == "win32":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        chocolatey = os.environ.get("ChocolateyInstall", r"C:\ProgramData\chocolatey")
        paths = [
            Path(program_files) / "mpv" / "mpv.exe",
            Path(program_files_x86) / "mpv" / "mpv.exe",
            Path(local) / "Programs" / "mpv" / "mpv.exe",
            Path(local) / "mpv" / "mpv.exe",
            Path(local) / "Microsoft" / "WinGet" / "Links" / "mpv.exe",
            home / "scoop" / "shims" / "mpv.exe",
            home / "scoop" / "apps" / "mpv" / "current" / "mpv.exe",
            Path(chocolatey) / "bin" / "mpv.exe",
        ]
        return [str(path) for path in paths]
    if sys.platform == "darwin":
        return [
            "/opt/homebrew/bin/mpv",
            "/usr/local/bin/mpv",
            str(home / "homebrew" / "bin" / "mpv"),
            "/Applications/mpv.app/Contents/MacOS/mpv",
            str(home / "Applications" / "mpv.app" / "Contents" / "MacOS" / "mpv"),
        ]
    return [
        "/usr/bin/mpv",
        "/usr/local/bin/mpv",
        str(home / ".local" / "bin" / "mpv"),
        "/snap/bin/mpv",
        str(home / ".nix-profile" / "bin" / "mpv"),
    ]


def find_mpv() -> str | None:
    """mpv on PATH, or a copy in a normal install folder."""

    found = shutil.which("mpv")
    if found:
        return found
    for candidate in _mpv_candidates():
        if Path(candidate).is_file():
            return candidate
    return None


def ipc_server(pid: int) -> str:
    """Address mpv should listen on. A named pipe on Windows, a socket elsewhere."""

    if sys.platform == "win32":
        return rf"\\.\pipe\tplay-{pid}"
    return str(Path(tempfile.gettempdir()) / f"tplay-{pid}.sock")


def popen_kwargs() -> dict[str, int | bool]:
    """Keep mpv out of this terminal's process group, on every platform."""

    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW. Numeric so this file
        # still imports on Linux, where those names are missing.
        return {"creationflags": 0x00000200 | 0x08000000}
    return {"start_new_session": True}


# mpv's pid while it should die with this process. None once it has exited.
_tracked_pid: int | None = None
_cleanup_installed = False
_win_close_handler: object | None = None

# A fresh interpreter sets this, then replaces itself with mpv. Doing it in
# the forked child is unsafe once the UI has other threads. The setting
# survives exec, so mpv still exits when tplay does. PR_SET_PDEATHSIG is 1.
_PARENT_DEATH = """\
import ctypes, os, signal, sys
libc = ctypes.CDLL(None, use_errno=True)
libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
libc.prctl.restype = ctypes.c_int
libc.prctl(1, int(signal.SIGTERM), 0, 0, 0)
if os.getppid() == 1:
    os.kill(os.getpid(), signal.SIGTERM)
os.execvp(sys.argv[1], sys.argv[1:])
"""


def tie_to_parent(command: list[str]) -> list[str]:
    """On Linux, make this program exit when tplay exits.

    Closing a terminal signals tplay, not mpv, because mpv has its own session.
    """

    if sys.platform != "linux":
        return list(command)
    return [sys.executable, "-c", _PARENT_DEATH, *command]


def track_player_process(pid: int) -> None:
    """Remember the player so a closed terminal can stop it."""

    global _tracked_pid
    _tracked_pid = pid
    _install_terminal_cleanup()


def forget_player_process(pid: int) -> None:
    """The player has exited. Do not signal that pid again."""

    global _tracked_pid
    if _tracked_pid == pid:
        _tracked_pid = None


def stop_tracked_player() -> None:
    """Stop the player process immediately. Safe to call more than once."""

    global _tracked_pid
    pid = _tracked_pid
    _tracked_pid = None
    if not pid or pid <= 0:
        return
    if sys.platform == "win32":
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return


def _on_terminal_closed(signum: int, _frame: object) -> None:
    """A closed terminal delivers SIGHUP. Stop audio, then exit."""

    stop_tracked_player()
    try:
        signal.signal(signum, signal.SIG_DFL)
    except (OSError, ValueError):
        os._exit(128 + signum)
    os.kill(os.getpid(), signum)
    os._exit(128 + signum)


def _install_windows_close_handler() -> None:
    """Stop audio when the console window closes. Windows only."""

    global _win_close_handler
    if _win_close_handler is not None:
        return
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def _handler(ctrl_type: int) -> bool:
        # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT.
        if ctrl_type in (2, 5, 6):
            stop_tracked_player()
        return False

    _win_close_handler = handler_type(_handler)
    kernel32.SetConsoleCtrlHandler.argtypes = [handler_type, wintypes.BOOL]
    kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
    if not kernel32.SetConsoleCtrlHandler(_win_close_handler, True):
        _win_close_handler = None


def _install_terminal_cleanup() -> None:
    global _cleanup_installed
    if _cleanup_installed:
        return
    _cleanup_installed = True
    atexit.register(stop_tracked_player)
    if sys.platform == "win32":
        try:
            _install_windows_close_handler()
        except (AttributeError, OSError):
            return
        return
    for sig in (signal.SIGHUP, signal.SIGTERM):
        try:
            signal.signal(sig, _on_terminal_closed)
        except (OSError, ValueError):
            continue


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
        self._ipc: _Ipc | None = None
        self._thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._snapshot = PlaybackSnapshot(volume=float(self._volume))
        self._live = False
        self._closed = False
        self._started = False
        pid = os.getpid()
        self.ipc_name = ipc_server(pid)
        self._log_path = Path(tempfile.gettempdir()) / f"tplay-{pid}.log"
        self._log_file: Any = None

    def start(self) -> None:
        if self._started:
            return
        mpv = find_mpv()
        if mpv is None:
            raise PlayerError(mpv_missing_message())
        _clear_ipc_name(self.ipc_name)
        self._log_file = open(self._log_path, "w", encoding="utf-8")
        command = [
            mpv,
            "--idle=yes",
            "--no-video",
            "--no-terminal",
            "--force-window=no",
            "--input-default-bindings=no",
            f"--input-ipc-server={self.ipc_name}",
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
                tie_to_parent(command),
                stdout=subprocess.DEVNULL,
                stderr=self._log_file,
                **popen_kwargs(),
            )
            track_player_process(self._proc.pid)
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
        if proc is not None and proc.poll() is not None:
            forget_player_process(proc.pid)
        self._close_ipc()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._release_ipc()
        self._close_log()
        _clear_ipc_name(self.ipc_name)

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
            if sys.platform != "win32" and not Path(self.ipc_name).exists():
                _sleep(0.05)
                continue
            ipc = _open_ipc()
            try:
                ipc.connect(self.ipc_name)
            except OSError as exc:
                last_error = str(exc)
                ipc.close()
                ipc.release()
            else:
                self._ipc = ipc
                return
            _sleep(0.05)
        detail = last_error or self.log_tail() or "timed out waiting for mpv"
        raise PlayerError(detail)

    def _observe(self) -> None:
        properties = ("time-pos", "duration", "pause", "volume", "idle-active")
        for index, name in enumerate(properties, start=1):
            self._send(["observe_property", index, name])

    def _read_loop(self) -> None:
        ipc = self._ipc
        if ipc is None:
            return
        buffer = b""
        while not self._closed:
            try:
                chunk = ipc.recv()
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
        ipc = self._ipc
        if ipc is None:
            return
        payload = json.dumps({"command": command}, ensure_ascii=False) + "\n"
        with self._write_lock:
            try:
                ipc.sendall(payload.encode("utf-8"))
            except OSError:
                return

    def _kill_proc(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        if sys.platform == "win32":
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
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

    def _close_ipc(self) -> None:
        ipc = self._ipc
        if ipc is None:
            return
        ipc.close()

    def _release_ipc(self) -> None:
        ipc = self._ipc
        self._ipc = None
        if ipc is None:
            return
        ipc.release()

    def _close_log(self) -> None:
        log_file = self._log_file
        self._log_file = None
        if log_file is None:
            return
        try:
            log_file.close()
        except OSError:
            pass


class _Ipc:
    def connect(self, name: str) -> None:
        raise NotImplementedError

    def recv(self) -> bytes:
        raise NotImplementedError

    def sendall(self, payload: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        return None

    def release(self) -> None:
        return None


class _UnixIpc(_Ipc):
    def __init__(self) -> None:
        self._sock: socket.socket | None = None

    def connect(self, name: str) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(name)
        except OSError:
            sock.close()
            raise
        self._sock = sock

    def recv(self) -> bytes:
        sock = self._sock
        if sock is None:
            return b""
        return sock.recv(65536)

    def sendall(self, payload: bytes) -> None:
        sock = self._sock
        if sock is None or not payload:
            return
        sock.sendall(payload)

    def close(self) -> None:
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

    def release(self) -> None:
        self.close()


def _open_ipc() -> _Ipc:
    if sys.platform == "win32":
        from tplay.winpipe import WinPipe

        return WinPipe()
    return _UnixIpc()


def _clear_ipc_name(name: str) -> None:
    if sys.platform == "win32":
        return
    try:
        Path(name).unlink(missing_ok=True)
    except OSError:
        return


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
