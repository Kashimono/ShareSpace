"""The initial Host Session / Join Session window."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from sharespace import __version__
from sharespace.platform.platform_manager import PlatformManager
from sharespace.ui.host_session_dialog import HostSessionDialog
from sharespace.ui.app_settings import apply_theme, load_preferences
from sharespace.ui.history_dialog import HistoryDialog
from sharespace.ui.resources import resource_path
from sharespace.ui.settings_dialog import SettingsDialog


class MainWindow(QMainWindow):
    """Present the two session entry points without starting networking."""

    def __init__(
        self, platform_manager: PlatformManager | None = None, *, store=None,
        preferences: dict[str, object] | None = None,
    ) -> None:
        super().__init__()
        self.setWindowIcon(QIcon(str(resource_path("sharespace.png"))))
        self._platform_manager = platform_manager
        self._store = store
        self._preferences = preferences or (load_preferences(store) if store is not None else {})
        if self._preferences:
            application = QApplication.instance()
            if application is not None:
                apply_theme(application, str(self._preferences.get("theme", "system")))
        self.setWindowTitle(f"ShareSpace {__version__}")
        self.setMinimumSize(680, 440)
        self.resize(840, 540)
        self._apply_window_style()

        page = QWidget()
        page.setObjectName("page")
        self.setCentralWidget(page)

        layout = QVBoxLayout(page)
        layout.setContentsMargins(48, 40, 48, 32)
        layout.setSpacing(14)

        eyebrow = QLabel("OFFLINE-FIRST FILE SHARING")
        eyebrow.setObjectName("eyebrow")
        layout.addWidget(eyebrow)

        title = QLabel("ShareSpace")
        title.setObjectName("title")
        layout.addWidget(title)

        subtitle = QLabel("Share files directly between nearby devices.")
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)
        layout.addSpacing(18)

        cards = QHBoxLayout()
        cards.setSpacing(16)
        cards.addWidget(
            self._create_action_card(
                title="Host a session",
                description="Host on an existing local network or a ShareSpace hotspot.",
                button_text="Host Session",
                status_text="",
                on_click=self._open_host_session,
            )
        )
        cards.addWidget(
            self._create_action_card(
                title="Join a session",
                description="Discover or manually connect to a nearby ShareSpace session.",
                button_text="Join Session",
                status_text="",
                on_click=self._open_join_session,
            )
        )
        layout.addLayout(cards)
        layout.addStretch(1)

        utilities = QHBoxLayout()
        self._transfer_history_button = QPushButton("Transfer History")
        self._transfer_history_button.clicked.connect(self._open_transfer_history)
        self._session_history_button = QPushButton("Session History")
        self._session_history_button.clicked.connect(self._open_session_history)
        self._settings_button = QPushButton("Settings")
        self._settings_button.clicked.connect(self._open_settings)
        utilities.addWidget(self._transfer_history_button)
        utilities.addWidget(self._session_history_button)
        utilities.addStretch(1)
        utilities.addWidget(self._settings_button)
        layout.addLayout(utilities)

        status_text = "Choose how you want to get started."
        if self._store is not None and not self._store.persistent:
            status_text = "Local history and settings are unavailable for this run."
        self._status = QLabel(status_text)
        self._status.setObjectName("status")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._status)

    def _create_action_card(
        self,
        *,
        title: str,
        description: str,
        button_text: str,
        status_text: str,
        on_click=None,
    ) -> QFrame:
        """Build one entry-point card and connect it to the status line."""
        card = QFrame()
        card.setObjectName("actionCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 22, 22, 22)
        card_layout.setSpacing(12)

        card_title = QLabel(title)
        card_title.setObjectName("cardTitle")
        card_layout.addWidget(card_title)

        card_description = QLabel(description)
        card_description.setObjectName("cardDescription")
        card_description.setWordWrap(True)
        card_layout.addWidget(card_description)
        card_layout.addStretch(1)

        button = QPushButton(button_text)
        if on_click is not None:
            button.clicked.connect(lambda _checked=False: on_click())
        else:
            button.clicked.connect(lambda _checked=False: self._status.setText(status_text))
        card_layout.addWidget(button)
        return card

    def _open_host_session(self) -> None:
        if self._platform_manager is None:
            from sharespace.platform.platform_manager import create_platform_manager

            self._platform_manager = create_platform_manager()
        dialog = HostSessionDialog(
            self._platform_manager, self, store=self._store, preferences=self._preferences
        )
        dialog.exec()

    def _open_join_session(self) -> None:
        from sharespace.ui.session_join_dialog import SessionJoinDialog

        dialog = SessionJoinDialog(self, store=self._store, preferences=self._preferences)
        dialog.exec()

    def _open_transfer_history(self) -> None:
        if self._store is not None:
            HistoryDialog(self._store, "transfers", self).exec()

    def _open_session_history(self) -> None:
        if self._store is not None:
            HistoryDialog(self._store, "sessions", self).exec()

    def _open_settings(self) -> None:
        if self._store is None:
            return
        try:
            dialog = SettingsDialog(self._store, self)
            dialog.settings_changed.connect(self._settings_changed)
            dialog.exec()
        except Exception:
            logging.getLogger(__name__).exception("Could not open application settings.")
            QMessageBox.warning(self, "Settings unavailable", "Settings could not be opened. Check local application storage.")

    def _settings_changed(self, preferences: object) -> None:
        if not isinstance(preferences, dict):
            return
        self._preferences = preferences
        application = QApplication.instance()
        if application is not None:
            apply_theme(application, str(preferences.get("theme", "system")))
        self._apply_window_style()

    def _apply_window_style(self) -> None:
        palette = QApplication.palette()
        window = palette.color(QPalette.ColorRole.Window).name()
        base = palette.color(QPalette.ColorRole.Base).name()
        text = palette.color(QPalette.ColorRole.WindowText).name()
        muted = palette.color(QPalette.ColorRole.PlaceholderText).name()
        border = palette.color(QPalette.ColorRole.Mid).name()
        accent = palette.color(QPalette.ColorRole.Highlight).name()
        hover = palette.color(QPalette.ColorRole.Dark).name()
        self.setStyleSheet(
            f"QMainWindow, QWidget#page {{ background: {window}; color: {text}; }}"
            f"QLabel#eyebrow {{ color: {accent}; font-size: 12px; font-weight: 700; }}"
            "QLabel#title { font-size: 32px; font-weight: 700; }"
            f"QLabel#subtitle, QLabel#cardDescription {{ color: {muted}; font-size: 14px; }}"
            f"QFrame#actionCard {{ background: {base}; border: 1px solid {border}; border-radius: 14px; }}"
            "QLabel#cardTitle { font-size: 19px; font-weight: 650; }"
            f"QPushButton {{ background: {accent}; border: 0; border-radius: 8px; color: white; "
            "font-size: 14px; font-weight: 650; min-height: 38px; padding: 0 14px; }"
            f"QPushButton:hover {{ background: {hover}; }}"
            f"QLabel#status {{ color: {muted}; font-size: 13px; }}"
        )
