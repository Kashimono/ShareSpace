"""Qt application startup and process lifetime."""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from sharespace.platform.platform_manager import create_platform_manager
from sharespace.ui.main_window import MainWindow


def main() -> int:
    """Create and run the ShareSpace desktop application."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    application = QApplication(sys.argv)
    application.setApplicationName("ShareSpace")
    application.setOrganizationName("ShareSpace")

    platform_manager = create_platform_manager()
    window = MainWindow(platform_manager=platform_manager)
    window.show()

    application.aboutToQuit.connect(platform_manager.cleanup_on_exit)
    return application.exec()
