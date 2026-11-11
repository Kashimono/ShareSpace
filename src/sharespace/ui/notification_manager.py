"""Small, session-scoped desktop notification helper with an in-app fallback."""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)


class NotificationManager:
    """Request native tray notices or show a short banner inside the dialog."""

    def __init__(
        self,
        parent: QWidget,
        layout: QVBoxLayout,
        *,
        native_owner: NotificationManager | None = None,
        preferences: dict[str, bool] | None = None,
    ) -> None:
        self.parent = parent
        self._native_owner = native_owner
        self._preferences = preferences or {}
        self._banner = QLabel(parent)
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet(
            "background: #eef4ff; color: #243b64; border: 1px solid #b8c9e6; "
            "border-radius: 6px; padding: 8px 10px;"
        )
        self._banner.hide()
        layout.insertWidget(1, self._banner)
        self._timer = QTimer(parent)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._banner.hide)
        self._tray: QSystemTrayIcon | None = None
        self._recent_ids: deque[str] = deque(maxlen=1024)
        self._recent_set: set[str] = set()

    def notify(
        self, title: str, message: str, *, event_id: str | None = None,
        category: str = "general",
    ) -> bool:
        """Show a unique notification; return whether native display was requested."""
        if self._preferences.get(category, True) is False:
            return False
        if event_id is not None:
            if event_id in self._recent_set:
                return False
            if len(self._recent_ids) == self._recent_ids.maxlen:
                self._recent_set.discard(self._recent_ids[0])
            self._recent_ids.append(event_id)
            self._recent_set.add(event_id)
        title = title[:80]
        message = message[:240]
        if self._notify_native(title, message):
            return True
        self._banner.setText(f"<b>{self._escape(title)}</b> — {self._escape(message)}")
        self._banner.show()
        self._timer.start(6000)
        return False

    def close(self) -> None:
        self._timer.stop()
        if self._tray is not None:
            self._tray.hide()
            self._tray = None
        self._banner.hide()

    def _notify_native(self, title: str, message: str) -> bool:
        if self._native_owner is not None:
            return self._native_owner._notify_native(title, message)
        if not QSystemTrayIcon.isSystemTrayAvailable() or not QSystemTrayIcon.supportsMessages():
            return False
        try:
            if self._tray is None:
                application = QApplication.instance()
                icon = application.windowIcon() if application else None
                if application is not None and (icon is None or icon.isNull()):
                    icon = application.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
                self._tray = QSystemTrayIcon(icon, self.parent)
                self._tray.setToolTip("ShareSpace session notifications")
                self._tray.show()
            self._tray.showMessage(
                title,
                message,
                QSystemTrayIcon.MessageIcon.Information,
                5000,
            )
            # Qt requests a platform notification; whether the desktop shows it
            # depends on its own tray and notification preferences.
            return True
        except Exception:
            return False

    @staticmethod
    def _escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
