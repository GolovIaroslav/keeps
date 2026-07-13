"""Global hotkey registration via the KGlobalAccel D-Bus service (KDE Plasma).

No KDE Frameworks Python bindings exist, so this talks to org.kde.kglobalaccel
directly over D-Bus -- the same mechanism KGlobalAccel::setShortcut() uses
internally in C++ (verified live against this machine's kglobalaccel, cross-
checked with CopyQ's own registration under /component/com_github_hluk_copyq).

PySide6's QDBusInterface.call() marshals a plain Python int list as an array
of variants, not the "ai" (array-of-int32) that setShortcut's "asaiu" D-Bus
signature requires -- confirmed by the resulting signature-mismatch error.
Building the array manually via QDBusArgument crashed the interpreter
(SIGABRT) on a QMetaType mismatch. `busctl`, which lets the signature be
stated explicitly, works reliably and ships with systemd -- an acceptable
dependency here since KGlobalAccel itself only exists on the (systemd-based)
KDE Plasma stack this backend targets.
"""

from __future__ import annotations

import logging
import subprocess

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage
from PySide6.QtGui import QKeySequence

logger = logging.getLogger(__name__)

SERVICE = "org.kde.kglobalaccel"
PATH = "/kglobalaccel"
INTERFACE = "org.kde.KGlobalAccel"
COMPONENT_INTERFACE = "org.kde.kglobalaccel.Component"
# KGlobalAccelD::SetShortcutFlags (kglobalacceld.h). SetPresent is what makes
# the daemon mark the shortcut "present" -> active -> actually grabbed by KWin;
# without it the key lands in kglobalshortcutsrc and the KCM but never fires
# (component stays isActive=false, verified live against kglobalacceld 6.7.2).
SET_PRESENT = 2
NO_AUTOLOADING = 4
DBUS_TIMEOUT_MS = 1_000
BUSCTL_TIMEOUT_SECONDS = 1

COMPONENT_UNIQUE = "keeps"
COMPONENT_FRIENDLY = "Keeps"
ACTION_UNIQUE = "toggle"
ACTION_FRIENDLY = "Toggle clipboard history"


def action_id(action_unique: str, action_friendly: str) -> list[str]:
    """KGlobalAccel's stable four-part action identity."""
    return [COMPONENT_UNIQUE, action_unique, COMPONENT_FRIENDLY, action_friendly]


ACTION_ID = action_id(ACTION_UNIQUE, ACTION_FRIENDLY)


def _set_shortcut_via_busctl(action: list[str], key_int: int) -> bool:
    command = [
        "busctl",
        "--user",
        "call",
        SERVICE,
        PATH,
        INTERFACE,
        "setShortcut",
        "asaiu",
        "4",
        *action,
        "1",
        str(key_int),
        str(SET_PRESENT | NO_AUTOLOADING),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=BUSCTL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("hotkey: setShortcut failed: %s", exc)
        return False
    return True


def _action_already_has_shortcut(action: list[str], key_int: int) -> bool:
    """Whether KGlobalAccel already stores `key_int` for this exact action.

    `isGlobalShortcutAvailable()` returns false for a shortcut owned by our
    action too. That is correct for a prospective action, but would make a
    durable per-clip shortcut fail to restore after the daemon restarts.
    """
    command = [
        "busctl",
        "--user",
        "call",
        SERVICE,
        PATH,
        INTERFACE,
        "shortcut",
        "as",
        "4",
        *action,
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=BUSCTL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("hotkey: shortcut lookup failed: %s", exc)
        return False
    return str(key_int) in result.stdout.split()


class KGlobalAccelHotkey(QObject):
    """Registers a global shortcut with KGlobalAccel; emits `triggered` on press."""

    triggered = Signal()

    def __init__(
        self,
        key_sequence: str = "Ctrl+`",
        parent: QObject | None = None,
        *,
        action_unique: str = ACTION_UNIQUE,
        action_friendly: str = ACTION_FRIENDLY,
    ) -> None:
        super().__init__(parent)
        self._key_sequence = key_sequence
        self._action_unique = action_unique
        self._action_friendly = action_friendly
        self._signal_connected = False
        self.last_error = ""

    @property
    def key_sequence(self) -> str:
        return self._key_sequence

    @property
    def action(self) -> list[str]:
        return action_id(self._action_unique, self._action_friendly)

    def register(self) -> bool:
        self.last_error = ""
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            logger.warning("hotkey: no session D-Bus connection")
            self.last_error = "no-session-dbus"
            return False

        kglobalaccel = QDBusInterface(SERVICE, PATH, INTERFACE, bus)
        kglobalaccel.setTimeout(DBUS_TIMEOUT_MS)
        if not kglobalaccel.isValid():
            logger.warning("hotkey: %s not available (not on Plasma?)", SERVICE)
            self.last_error = "kglobalaccel-unavailable"
            return False

        sequence = QKeySequence(self._key_sequence)
        if sequence.isEmpty():
            logger.warning("hotkey: invalid key sequence %r", self._key_sequence)
            self.last_error = "invalid"
            return False
        key_int = sequence[0].toCombined()
        action = self.action
        if self._action_unique != ACTION_UNIQUE:
            available = kglobalaccel.call(
                "isGlobalShortcutAvailable", key_int, COMPONENT_UNIQUE
            )
            if available.type() == QDBusMessage.MessageType.ErrorMessage:
                logger.warning("hotkey: availability check failed: %s", available.errorMessage())
                self.last_error = "availability-check-failed"
                return False
            available_args = available.arguments()
            if (not available_args or not available_args[0]) and not _action_already_has_shortcut(
                action, key_int
            ):
                self.last_error = "conflict"
                return False

        registration_reply = kglobalaccel.call("doRegister", action)
        if registration_reply.type() == QDBusMessage.MessageType.ErrorMessage:
            logger.warning("hotkey: doRegister failed: %s", registration_reply.errorMessage())
            self.last_error = "registration-failed"
            return False
        component_reply = kglobalaccel.call("getComponent", COMPONENT_UNIQUE)
        if component_reply.type() == QDBusMessage.MessageType.ErrorMessage:
            logger.warning("hotkey: getComponent failed: %s", component_reply.errorMessage())
            self.last_error = "component-failed"
            return False
        component_path = component_reply.arguments()[0].path()

        # SLOT()-macro string form: a leading "1" marks it as a slot (vs "2"
        # for a signal) to QDBusConnection.connect's string-based lookup.
        if not self._signal_connected:
            connected = bus.connect(
                SERVICE,
                component_path,
                COMPONENT_INTERFACE,
                "globalShortcutPressed",
                self,
                "1_on_pressed(QString,QString,qlonglong)",
            )
            if not connected:
                logger.warning("hotkey: failed to connect globalShortcutPressed signal")
                self.last_error = "signal-connection-failed"
                return False
            self._signal_connected = True
        if not _set_shortcut_via_busctl(action, key_int):
            self.last_error = "registration-failed"
            return False
        return True

    def set_key_sequence(self, key_sequence: str) -> bool:
        """Replace this action's key and restore the old one if registration fails."""
        if key_sequence == self._key_sequence:
            return True
        previous = self._key_sequence
        self._key_sequence = key_sequence
        if self.register():
            return True
        self._key_sequence = previous
        previous_sequence = QKeySequence(previous)
        if not previous_sequence.isEmpty():
            _set_shortcut_via_busctl(self.action, previous_sequence[0].toCombined())
        return False

    def unregister(self, *, remove: bool = False) -> None:
        # setInactive releases the grab but keeps the binding in
        # kglobalshortcutsrc; per-clip removal uses unRegister to erase the
        # action after its clip was deleted or cleared.
        kglobalaccel = QDBusInterface(SERVICE, PATH, INTERFACE, QDBusConnection.sessionBus())
        kglobalaccel.setTimeout(DBUS_TIMEOUT_MS)
        if kglobalaccel.isValid():
            kglobalaccel.call("unRegister" if remove else "setInactive", self.action)

    @Slot(str, str, "qlonglong")
    def _on_pressed(self, component: str, action: str, _timestamp: int) -> None:
        if component == COMPONENT_UNIQUE and action == self._action_unique:
            self.triggered.emit()
