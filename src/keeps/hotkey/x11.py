"""Global hotkey registration via XGrabKey (plain X11, non-KDE fallback).

KGlobalAccel (hotkey/kglobalaccel.py) is tried first since it works on any
KDE Plasma session (X11 or Wayland). This backend only matters on non-KDE
X11 desktops. There is no Qt API for a *global* key grab -- Qt's own X11 QPA
backend only delivers key events to Qt's own windows -- so this talks to
libX11 directly via ctypes, the same way any native X11 hotkey daemon does.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging

from PySide6.QtCore import QObject, QSocketNotifier, Signal

from keeps.hotkey.base import parse_key_sequence

logger = logging.getLogger(__name__)

KEY_PRESS = 2
LOCK_MASK = 1 << 1  # Caps Lock
NUM_LOCK_MASK = 1 << 4  # Num Lock (Mod2, standard on most X servers)
# X11 folds Caps/Num Lock into the effective modifier state, so a grab for
# just Ctrl+` only fires when both happen to be off. Grab every combination.
_LOCK_COMBINATIONS = (0, LOCK_MASK, NUM_LOCK_MASK, LOCK_MASK | NUM_LOCK_MASK)
_XEVENT_SIZE = 192  # sizeof(XEvent) on 64-bit Xlib (union padded to 24 longs)

_lib_cache: ctypes.CDLL | None = None


class _XKeyEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("keycode", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


def _lib() -> ctypes.CDLL:
    global _lib_cache
    if _lib_cache is not None:
        return _lib_cache
    path = ctypes.util.find_library("X11")
    if path is None:
        raise OSError("libX11 not found")
    lib = ctypes.CDLL(path)
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XDefaultRootWindow.restype = ctypes.c_ulong
    lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    lib.XStringToKeysym.restype = ctypes.c_ulong
    lib.XStringToKeysym.argtypes = [ctypes.c_char_p]
    lib.XKeysymToKeycode.restype = ctypes.c_ubyte
    lib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib.XGrabKey.restype = ctypes.c_int
    lib.XGrabKey.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ]
    lib.XUngrabKey.restype = ctypes.c_int
    lib.XUngrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong]
    lib.XConnectionNumber.restype = ctypes.c_int
    lib.XConnectionNumber.argtypes = [ctypes.c_void_p]
    lib.XPending.restype = ctypes.c_int
    lib.XPending.argtypes = [ctypes.c_void_p]
    lib.XNextEvent.restype = ctypes.c_int
    lib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.XFlush.restype = ctypes.c_int
    lib.XFlush.argtypes = [ctypes.c_void_p]
    lib.XCloseDisplay.restype = ctypes.c_int
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    _lib_cache = lib
    return lib


class XGrabKeyHotkey(QObject):
    """Registers a global shortcut via XGrabKey; emits `triggered` on press."""

    triggered = Signal()

    def __init__(self, key_sequence: str = "Ctrl+`", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._key_sequence = key_sequence
        self._display = None
        self._root = None
        self._keycode = None
        self._base_mask = None
        self._notifier: QSocketNotifier | None = None

    def register(self) -> bool:
        try:
            lib = _lib()
        except OSError as exc:
            logger.warning("hotkey: %s", exc)
            return False

        display = lib.XOpenDisplay(None)
        if not display:
            logger.warning("hotkey: XOpenDisplay failed (no X11 display?)")
            return False

        try:
            mask, keysym_name = parse_key_sequence(self._key_sequence)
        except (KeyError, ValueError) as exc:
            logger.warning("hotkey: cannot parse key sequence %r: %s", self._key_sequence, exc)
            lib.XCloseDisplay(display)
            return False

        keysym = lib.XStringToKeysym(keysym_name.encode())
        if keysym == 0:
            logger.warning("hotkey: unknown key %r", keysym_name)
            lib.XCloseDisplay(display)
            return False
        keycode = lib.XKeysymToKeycode(display, keysym)
        if keycode == 0:
            logger.warning("hotkey: no keycode for %r", keysym_name)
            lib.XCloseDisplay(display)
            return False

        root = lib.XDefaultRootWindow(display)
        for lock_bits in _LOCK_COMBINATIONS:
            lib.XGrabKey(display, keycode, mask | lock_bits, root, True, 1, 1)
        lib.XFlush(display)

        self._display = display
        self._root = root
        self._keycode = keycode
        self._base_mask = mask

        fd = lib.XConnectionNumber(display)
        self._notifier = QSocketNotifier(fd, QSocketNotifier.Type.Read, self)
        self._notifier.activated.connect(self._on_readable)
        return True

    def _on_readable(self) -> None:
        lib = _lib()
        buf = ctypes.create_string_buffer(_XEVENT_SIZE)
        while lib.XPending(self._display):
            lib.XNextEvent(self._display, buf)
            event = ctypes.cast(buf, ctypes.POINTER(_XKeyEvent)).contents
            if event.type != KEY_PRESS or event.keycode != self._keycode:
                continue
            effective = event.state & ~(LOCK_MASK | NUM_LOCK_MASK)
            if effective == self._base_mask:
                self.triggered.emit()

    def unregister(self) -> None:
        if self._display is None:
            return
        lib = _lib()
        if self._notifier is not None:
            self._notifier.setEnabled(False)
            self._notifier = None
        for lock_bits in _LOCK_COMBINATIONS:
            lib.XUngrabKey(self._display, self._keycode, self._base_mask | lock_bits, self._root)
        lib.XCloseDisplay(self._display)
        self._display = None
