"""Qt application startup and process lifetime."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from sharespace.ui.main_window import MainWindow


def main() -> int:
    """Create and run the ShareSpace desktop application."""
    application = QApplication(sys.argv)
    application.setApplicationName("ShareSpace")
    application.setOrganizationName("ShareSpace")

    window = MainWindow()
    window.show()

    return application.exec()
