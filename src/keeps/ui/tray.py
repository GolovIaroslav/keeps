"""System tray icon: show popup, pause capture, open settings, quit."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class TrayIcon(QObject):
    show_requested = Signal()
    settings_requested = Signal()
    quit_requested = Signal()
    capture_paused_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tray = QSystemTrayIcon(QIcon.fromTheme("edit-paste"), self)
        self._tray.setToolTip("Keeps")

        menu = QMenu()
        show_action = menu.addAction(self.tr("Show"))
        show_action.triggered.connect(self.show_requested)

        self._pause_action = menu.addAction(self.tr("Pause capture"))
        self._pause_action.setCheckable(True)
        self._pause_action.toggled.connect(self.capture_paused_changed)

        menu.addSeparator()
        settings_action = menu.addAction(self.tr("Settings..."))
        settings_action.triggered.connect(self.settings_requested)

        menu.addSeparator()
        quit_action = menu.addAction(self.tr("Quit"))
        quit_action.triggered.connect(self.quit_requested)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_activated)

    def show(self) -> None:
        self._tray.show()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_requested.emit()
