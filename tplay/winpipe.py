"""Windows named-pipe client for mpv's JSON IPC.

mpv on Windows listens on ``\\\\.\\pipe\\...`` and only allows a client to read
and write at the same time when the pipe is opened with overlapped I/O.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_FILE_FLAG_OVERLAPPED = 0x40000000
_ERROR_IO_PENDING = 997
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232
_ERROR_PIPE_NOT_CONNECTED = 233
_ERROR_OPERATION_ABORTED = 995
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_QUIET = {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA, _ERROR_PIPE_NOT_CONNECTED, _ERROR_OPERATION_ABORTED}

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", ctypes.c_void_p),
    ]


_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
]
_kernel32.CreateFileW.restype = ctypes.c_void_p
_kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = ctypes.c_void_p
_kernel32.ReadFile.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(_Overlapped),
]
_kernel32.ReadFile.restype = wintypes.BOOL
_kernel32.WriteFile.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(_Overlapped),
]
_kernel32.WriteFile.restype = wintypes.BOOL
_kernel32.GetOverlappedResult.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(_Overlapped),
    ctypes.POINTER(wintypes.DWORD),
    wintypes.BOOL,
]
_kernel32.GetOverlappedResult.restype = wintypes.BOOL
_kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Overlapped)]
_kernel32.CancelIoEx.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD


def _bad_handle(handle: object) -> bool:
    if not handle:
        return True
    value = int(handle)  # type: ignore[arg-type]
    return value in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)


class WinPipe:
    """One duplex connection to an mpv named pipe."""

    def __init__(self) -> None:
        self._handle: int | None = None
        self._closed = False

    def connect(self, name: str) -> None:
        handle = _kernel32.CreateFileW(
            name,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OVERLAPPED,
            None,
        )
        if _bad_handle(handle):
            error = ctypes.get_last_error()
            raise OSError(error, f"could not open {name}")
        self._handle = int(handle)  # type: ignore[arg-type]
        self._closed = False

    def recv(self) -> bytes:
        handle = self._handle
        if handle is None or self._closed:
            return b""
        buffer = ctypes.create_string_buffer(65536)
        overlapped = _Overlapped()
        event = _kernel32.CreateEventW(None, True, False, None)
        if _bad_handle(event):
            raise OSError(ctypes.get_last_error(), "could not wait on the mpv pipe")
        overlapped.hEvent = event
        got = wintypes.DWORD(0)
        try:
            ok = _kernel32.ReadFile(
                handle,
                ctypes.cast(buffer, ctypes.c_void_p),
                65536,
                ctypes.byref(got),
                ctypes.byref(overlapped),
            )
            if not ok:
                error = ctypes.get_last_error()
                if error in _QUIET:
                    return b""
                if error != _ERROR_IO_PENDING:
                    raise OSError(error, "could not read the mpv pipe")
                while True:
                    if self._closed:
                        _kernel32.CancelIoEx(handle, ctypes.byref(overlapped))
                    status = _kernel32.WaitForSingleObject(event, 200)
                    if status == _WAIT_OBJECT_0:
                        break
                    if status != _WAIT_TIMEOUT or self._closed:
                        _kernel32.CancelIoEx(handle, ctypes.byref(overlapped))
                        return b""
                if not _kernel32.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(got), True):
                    return b""
            return buffer.raw[: got.value]
        finally:
            _kernel32.CloseHandle(event)

    def sendall(self, payload: bytes) -> None:
        handle = self._handle
        if handle is None or self._closed or not payload:
            return
        view = payload
        while view:
            buffer = ctypes.create_string_buffer(view)
            overlapped = _Overlapped()
            event = _kernel32.CreateEventW(None, True, False, None)
            if _bad_handle(event):
                raise OSError(ctypes.get_last_error(), "could not write to the mpv pipe")
            overlapped.hEvent = event
            wrote = wintypes.DWORD(0)
            try:
                ok = _kernel32.WriteFile(
                    handle,
                    ctypes.cast(buffer, ctypes.c_void_p),
                    len(view),
                    ctypes.byref(wrote),
                    ctypes.byref(overlapped),
                )
                if not ok:
                    error = ctypes.get_last_error()
                    if error != _ERROR_IO_PENDING:
                        raise OSError(error, "could not write to the mpv pipe")
                    if not _kernel32.GetOverlappedResult(
                        handle, ctypes.byref(overlapped), ctypes.byref(wrote), True
                    ):
                        raise OSError(ctypes.get_last_error(), "could not write to the mpv pipe")
            finally:
                _kernel32.CloseHandle(event)
            sent = int(wrote.value)
            if sent <= 0:
                raise OSError("could not write to the mpv pipe")
            view = view[sent:]

    def close(self) -> None:
        """Unblock a pending read. The handle is closed in ``release``."""

        self._closed = True
        handle = self._handle
        if handle:
            _kernel32.CancelIoEx(handle, None)

    def release(self) -> None:
        self._closed = True
        handle = self._handle
        self._handle = None
        if not handle:
            return
        _kernel32.CancelIoEx(handle, None)
        _kernel32.CloseHandle(handle)
