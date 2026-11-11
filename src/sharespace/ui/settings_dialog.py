"""Central application preferences dialog."""

from __future__ import annotations

import os
import logging
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from sharespace.ui.app_settings import (
    NOTIFICATION_KEYS, RETENTION_OPTIONS, THEMES, load_preferences,
)

logger = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    """Edit preferences that affect only local presentation and file destinations."""

    settings_changed = Signal(object)

    def __init__(self, store, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.preferences = load_preferences(store)
        self.setWindowTitle("ShareSpace settings")
        self.resize(560, 520)
        layout = QVBoxLayout(self)
        heading = QLabel("Settings")
        heading.setStyleSheet("font-size: 22px; font-weight: 700;")
        layout.addWidget(heading)
        form = QFormLayout()
        self._theme = QComboBox()
        for value in THEMES:
            self._theme.addItem(value.title(), value)
        self._theme.setCurrentIndex(max(0, self._theme.findData(self.preferences["theme"])))
        form.addRow("Appearance", self._theme)
        self._download_path = QLineEdit(str(self.preferences["download_directory"]))
        self._download_path.setReadOnly(True)
        choose = QPushButton("Choose folder…")
        choose.clicked.connect(self._choose_download_directory)
        row = QHBoxLayout()
        row.addWidget(self._download_path, 1)
        row.addWidget(choose)
        download_widget = QWidget()
        download_widget.setLayout(row)
        form.addRow("Default download folder", download_widget)
        self._retention = QComboBox()
        for value in RETENTION_OPTIONS:
            label = "Keep indefinitely" if value == 0 else f"Keep {value} days"
            self._retention.addItem(label, value)
        self._retention.setCurrentIndex(max(0, self._retention.findData(self.preferences["history_retention_days"])))
        form.addRow("History retention", self._retention)
        layout.addLayout(form)
        layout.addWidget(QLabel("Notifications"))
        notifications = self.preferences["notifications"]
        self._notification_boxes: dict[str, QCheckBox] = {}
        labels = {
            "file_created": "Shared file created",
            "file_modified": "Shared file modified",
            "file_deleted": "Shared file deleted",
            "participant_activity": "Participant joined or left",
            "transfer_completion": "Transfer completed",
            "transfer_failure": "Transfer failed",
        }
        for key in NOTIFICATION_KEYS:
            checkbox = QCheckBox(labels[key])
            checkbox.setChecked(bool(notifications.get(key, True)))
            layout.addWidget(checkbox)
            self._notification_boxes[key] = checkbox
        layout.addStretch(1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save settings")
        save.clicked.connect(self._save)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

    def _choose_download_directory(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose download folder", self._download_path.text()
        )
        if not chosen:
            return
        path = Path(chosen).expanduser()
        if not path.is_dir() or not os.access(path, os.W_OK):
            QMessageBox.warning(self, "Folder unavailable", "Choose a folder that exists and is writable.")
            return
        self._download_path.setText(str(path))

    def _save(self) -> None:
        path = Path(self._download_path.text()).expanduser()
        if not path.is_dir() or not os.access(path, os.W_OK):
            QMessageBox.warning(self, "Folder unavailable", "Choose a folder that exists and is writable.")
            return
        preferences = {
            "theme": self._theme.currentData(),
            "download_directory": str(path),
            "history_retention_days": self._retention.currentData(),
            "notifications": {
                key: checkbox.isChecked() for key, checkbox in self._notification_boxes.items()
            },
        }
        try:
            self.store.set_settings(preferences)
            self.store.apply_retention(int(preferences["history_retention_days"]))
        except Exception:
            logger.exception("Could not persist application preferences.")
            QMessageBox.warning(self, "Settings unavailable", "Settings could not be saved. Check local application storage and try again.")
            return
        self.preferences = preferences
        self.settings_changed.emit(preferences)
        self.accept()
