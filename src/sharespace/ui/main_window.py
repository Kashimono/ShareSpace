"""The initial Host Session / Join Session window."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sharespace.platform.platform_manager import PlatformManager
from sharespace.ui.host_session_dialog import HostSessionDialog


class MainWindow(QMainWindow):
    """Present the two session entry points without starting networking."""

    def __init__(self, platform_manager: PlatformManager | None = None) -> None:
        super().__init__()
        self._platform_manager = platform_manager
        self.setWindowTitle("ShareSpace")
        self.setMinimumSize(680, 440)
        self.resize(840, 540)
        self.setStyleSheet(
            """
            QMainWindow, QWidget#page { background: #f5f7fb; color: #172033; }
            QLabel#eyebrow { color: #4357c5; font-size: 12px; font-weight: 700; }
            QLabel#title { font-size: 32px; font-weight: 700; }
            QLabel#subtitle { color: #647087; font-size: 15px; }
            QFrame#actionCard {
                background: #ffffff;
                border: 1px solid #e1e6ef;
                border-radius: 14px;
            }
            QLabel#cardTitle { font-size: 19px; font-weight: 650; }
            QLabel#cardDescription { color: #647087; font-size: 14px; }
            QPushButton {
                background: #4357c5;
                border: 0;
                border-radius: 8px;
                color: #ffffff;
                font-size: 14px;
                font-weight: 650;
                min-height: 42px;
                padding: 0 16px;
            }
            QPushButton:hover { background: #3548ae; }
            QPushButton:pressed { background: #293a96; }
            QLabel#status { color: #647087; font-size: 13px; }
            """
        )

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
                description="Create a local Wi-Fi network for nearby devices.",
                button_text="Host Session",
                status_text="",
                on_click=self._open_host_session,
            )
        )
        cards.addWidget(
            self._create_action_card(
                title="Join a session",
                description="Connect to a ShareSpace session on a nearby device.",
                button_text="Join Session",
                status_text="Joining a session will be available in a later milestone.",
            )
        )
        layout.addLayout(cards)
        layout.addStretch(1)

        self._status = QLabel("Choose how you want to get started.")
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
        dialog = HostSessionDialog(self._platform_manager, self)
        dialog.exec()
