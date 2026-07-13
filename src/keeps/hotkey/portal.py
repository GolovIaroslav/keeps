"""Optional XDG GlobalShortcuts portal backend for non-KDE Wayland sessions.

The portal methods use the system ``gdbus`` helper because PySide6's generic
QDBus marshalling is awkward for ``a(sa{sv})``.  Signal delivery remains on
Qt's D-Bus connection, so the daemon never polls keyboard state.
"""

from __future__ import annotations

import re
import subprocess
import uuid

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtDBus import QDBusConnection, QDBusInterface

from keeps.hotkey.portal_format import xdg_trigger

PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_INTERFACE = "org.freedesktop.portal.GlobalShortcuts"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"
GDBUS_TIMEOUT_SECONDS = 3


def _object_path(output: str) -> str | None:
    match = re.search(r"(/org/[A-Za-z0-9_./-]+)", output)
    return match.group(1) if match else None


def _unwrap(value):
    variant = getattr(value, "variant", None)
    if callable(variant):
        return _unwrap(variant())
    return value


class _PortalRequest(QObject):
    completed = Signal(int, object)

    @Slot("uint", "QVariantMap")
    def _on_response(self, response: int, results: dict) -> None:
        self.completed.emit(response, results)


class PortalGlobalShortcutHotkey(QObject):
    """Register one global shortcut through XDG GlobalShortcuts."""

    triggered = Signal()
    registration_failed = Signal(str)

    def __init__(
        self, key_sequence: str = "Ctrl+`", parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._key_sequence = key_sequence
        self._session: str | None = None
        self._ready = False
        self._requests: dict[str, _PortalRequest] = {}
        self._signal_connected = False
        self.last_error = ""

    @property
    def key_sequence(self) -> str:
        return self._key_sequence

    def register(self) -> bool:
        self.last_error = ""
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            self.last_error = "no-session-dbus"
            return False
        portal = QDBusInterface(
            PORTAL_SERVICE, PORTAL_PATH, PORTAL_INTERFACE, bus
        )
        if not portal.isValid():
            self.last_error = "portal-unavailable"
            return False
        try:
            trigger = xdg_trigger(self._key_sequence)
        except (KeyError, ValueError):
            self.last_error = "invalid"
            return False
        self._pending_trigger = trigger
        if not self._signal_connected:
            connected = bus.connect(
                PORTAL_SERVICE,
                PORTAL_PATH,
                PORTAL_INTERFACE,
                "Activated",
                self,
                "1_on_activated(QString,QString,qulonglong,QVariantMap)",
            )
            if not connected:
                self.last_error = "signal-connection-failed"
                return False
            self._signal_connected = True

        token = f"keeps_{uuid.uuid4().hex}"
        request = self._call(
            "CreateSession",
            f"{{'handle_token': <'{token}_request'>, 'session_handle_token': <'{token}_session'>}}",
        )
        if request is None:
            self.last_error = "create-session-failed"
            return False
        self._connect_request(request, self._on_session_created)
        # The portal call is asynchronous after this request object is returned.
        return True

    def _call(self, method: str, *arguments: str) -> str | None:
        command = [
            "gdbus",
            "call",
            "--session",
            "--dest",
            PORTAL_SERVICE,
            "--object-path",
            PORTAL_PATH,
            "--method",
            f"{PORTAL_INTERFACE}.{method}",
            *arguments,
        ]
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=GDBUS_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        return _object_path(result.stdout)

    def _connect_request(self, path: str, handler) -> None:
        request = _PortalRequest(self)
        request.completed.connect(handler)
        self._requests[path] = request
        QDBusConnection.sessionBus().connect(
            PORTAL_SERVICE,
            path,
            REQUEST_INTERFACE,
            "Response",
            request,
            "1_on_response(uint,QVariantMap)",
        )

    def _on_session_created(self, response: int, results: object) -> None:
        if response != 0:
            self.last_error = "session-denied"
            self.registration_failed.emit(self.last_error)
            return
        values = _unwrap(results)
        session = _unwrap(values.get("session_handle")) if isinstance(values, dict) else None
        if not isinstance(session, str):
            self.last_error = "session-response-invalid"
            self.registration_failed.emit(self.last_error)
            return
        self._session = session
        trigger = getattr(self, "_pending_trigger", "")
        token = f"keeps_{uuid.uuid4().hex}"
        shortcuts = (
            f"[('toggle', {{'description': <'{self.tr('Toggle clipboard history')}'>, "
            f"'preferred_trigger': <'{trigger}'>}})]"
        )
        request = self._call(
            "BindShortcuts",
            session,
            shortcuts,
            "",
            f"{{'handle_token': <'{token}'>}}",
        )
        if request is None:
            self.last_error = "bind-failed"
            self.registration_failed.emit(self.last_error)
            return
        self._connect_request(request, self._on_shortcuts_bound)

    def _on_shortcuts_bound(self, response: int, _results: object) -> None:
        if response == 0:
            self._ready = True
        else:
            self.last_error = "shortcut-denied"
            self.registration_failed.emit(self.last_error)

    @Slot(str, str, "qulonglong", "QVariantMap")
    def _on_activated(
        self, session: str, shortcut_id: str, _timestamp: int, _options: dict
    ) -> None:
        if self._ready and session == self._session and shortcut_id == "toggle":
            self.triggered.emit()

    def set_key_sequence(self, _key_sequence: str) -> bool:
        """Portal bindings are one-shot; re-register from a fresh daemon instead."""
        self.last_error = "restart-required"
        return False

    def unregister(self, *, remove: bool = False) -> None:
        del remove
        if self._session is not None:
            self._call_session_close(self._session)
        self._session = None
        self._ready = False

    @staticmethod
    def _call_session_close(session: str) -> None:
        command = [
            "gdbus",
            "call",
            "--session",
            "--dest",
            PORTAL_SERVICE,
            "--object-path",
            session,
            "--method",
            f"{SESSION_INTERFACE}.Close",
        ]
        try:
            subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=GDBUS_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
