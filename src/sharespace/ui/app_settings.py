"""Validated user preferences and consistent Qt theme application."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)
THEMES = ("system", "light", "dark")
RETENTION_OPTIONS = (0, 30, 90, 365)
NOTIFICATION_KEYS = (
    "file_created", "file_modified", "file_deleted", "participant_activity",
    "transfer_completion", "transfer_failure",
)


def default_download_directory() -> str:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    return location or str(Path.home())


def default_preferences() -> dict[str, object]:
    return {
        "theme": "system",
        "notifications": {key: True for key in NOTIFICATION_KEYS},
        "download_directory": default_download_directory(),
        "history_retention_days": 365,
    }


def load_preferences(store) -> dict[str, object]:
    """Load values individually and fall back safely when a stored value is invalid."""
    defaults = default_preferences()
    loaded = store.settings(defaults)
    theme = loaded.get("theme")
    if theme not in THEMES:
        logger.warning("Ignoring an invalid stored theme preference.")
        theme = defaults["theme"]
    notifications = loaded.get("notifications")
    if not isinstance(notifications, dict):
        notifications = defaults["notifications"]
    notifications = {
        key: bool(notifications.get(key, True)) for key in NOTIFICATION_KEYS
    }
    directory = loaded.get("download_directory")
    if not isinstance(directory, str) or not directory:
        directory = str(defaults["download_directory"])
    else:
        try:
            target = Path(directory).expanduser()
            if not target.is_dir() or not os.access(target, os.W_OK):
                directory = str(defaults["download_directory"])
        except (OSError, ValueError):
            directory = str(defaults["download_directory"])
    retention = loaded.get("history_retention_days")
    if isinstance(retention, bool) or retention not in RETENTION_OPTIONS:
        retention = defaults["history_retention_days"]
    return {
        "theme": theme,
        "notifications": notifications,
        "download_directory": directory,
        "history_retention_days": retention,
    }


def apply_theme(application: QApplication, theme: str) -> None:
    """Apply a palette consistently while honoring native system defaults."""
    if theme not in THEMES:
        theme = "system"
    if theme == "system":
        application.setPalette(QApplication.style().standardPalette())
        application.setStyleSheet("")
        return

    dark = theme == "dark"
    colors = {
        "window": "#1e232b" if dark else "#f5f7fb",
        "base": "#171b21" if dark else "#ffffff",
        "alternate": "#242a33" if dark else "#f1f4f9",
        "text": "#edf1f7" if dark else "#172033",
        "muted": "#aab4c2" if dark else "#647087",
        "button": "#303844" if dark else "#ffffff",
        "highlight": "#7488ee" if dark else "#4357c5",
        "highlighted_text": "#ffffff",
    }
    palette = QPalette()
    for role, value in (
        (QPalette.ColorRole.Window, colors["window"]),
        (QPalette.ColorRole.Base, colors["base"]),
        (QPalette.ColorRole.AlternateBase, colors["alternate"]),
        (QPalette.ColorRole.Text, colors["text"]),
        (QPalette.ColorRole.WindowText, colors["text"]),
        (QPalette.ColorRole.Button, colors["button"]),
        (QPalette.ColorRole.ButtonText, colors["text"]),
        (QPalette.ColorRole.Highlight, colors["highlight"]),
        (QPalette.ColorRole.HighlightedText, colors["highlighted_text"]),
        (QPalette.ColorRole.ToolTipBase, colors["base"]),
        (QPalette.ColorRole.ToolTipText, colors["text"]),
        (QPalette.ColorRole.Link, colors["highlight"]),
    ):
        palette.setColor(role, QColor(value))
    disabled = QColor(colors["muted"])
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    application.setPalette(palette)
    application.setStyleSheet(
        f"QToolTip {{ color: {colors['text']}; background: {colors['base']}; "
        f"border: 1px solid {colors['muted']}; }}"
    )
