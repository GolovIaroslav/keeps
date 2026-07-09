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
NO_AUTOLOADING = 4  # KGlobalAccel::NoAutoloading, see KF6/KGlobalAccel/kglobalaccel.h

COMPONENT_UNIQUE = "keeps"
COMPONENT_FRIENDLY = "Keeps"
ACTION_UNIQUE = "toggle"
ACTION_FRIENDLY = "Toggle clipboard history"
ACTION_ID = [COMPONENT_UNIQUE, ACTION_UNIQUE, COMPONENT_FRIENDLY, ACTION_FRIENDLY]


def _set_shortcut_via_busctl(key_int: int) -> bool:
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
        *ACTION_ID,
        "1",
        str(key_int),
        str(NO_AUTOLOADING),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("hotkey: setShortcut failed: %s", exc)
        return False
    return True


class KGlobalAccelHotkey(QObject):
    """Registers a global shortcut with KGlobalAccel; emits `triggered` on press."""

    triggered = Signal()

    def __init__(self, key_sequence: str = "Ctrl+`", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._key_sequence = key_sequence

    def register(self) -> bool:
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            logger.warning("hotkey: no session D-Bus connection")
            return False

        kglobalaccel = QDBusInterface(SERVICE, PATH, INTERFACE, bus)
        if not kglobalaccel.isValid():
            logger.warning("hotkey: %s not available (not on Plasma?)", SERVICE)
            return False

        kglobalaccel.call("doRegister", ACTION_ID)

        sequence = QKeySequence(self._key_sequence)
        if sequence.isEmpty():
            logger.warning("hotkey: invalid key sequence %r", self._key_sequence)
            return False
        if not _set_shortcut_via_busctl(sequence[0].toCombined()):
            return False

        component_reply = kglobalaccel.call("getComponent", COMPONENT_UNIQUE)
        if component_reply.type() == QDBusMessage.MessageType.ErrorMessage:
            logger.warning("hotkey: getComponent failed: %s", component_reply.errorMessage())
            return False
        component_path = component_reply.arguments()[0].path()

        # SLOT()-macro string form: a leading "1" marks it as a slot (vs "2"
        # for a signal) to QDBusConnection.connect's string-based lookup.
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
            return False
        return True

    def unregister(self) -> None:
        kglobalaccel = QDBusInterface(SERVICE, PATH, INTERFACE, QDBusConnection.sessionBus())
        if kglobalaccel.isValid():
            kglobalaccel.call("unRegister", ACTION_ID)

    @Slot(str, str, "qlonglong")
    def _on_pressed(self, component: str, action: str, _timestamp: int) -> None:
        if component == COMPONENT_UNIQUE and action == ACTION_UNIQUE:
            self.triggered.emit()
