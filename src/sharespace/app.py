"""Qt application startup and process lifetime."""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from sharespace.platform.platform_manager import create_platform_manager
from sharespace.storage.local_store import LocalStore
from sharespace.ui.app_settings import load_preferences
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

    def unhandled_exception(exception_type, exception, traceback) -> None:
        if issubclass(exception_type, KeyboardInterrupt):
            sys.__excepthook__(exception_type, exception, traceback)
            return
        logging.getLogger("sharespace").critical(
            "Unhandled application exception.", exc_info=(exception_type, exception, traceback)
        )
        QMessageBox.critical(
            None, "ShareSpace error",
            "An unexpected error interrupted the application. Restart ShareSpace; "
            "diagnostic details were saved to the application log.",
        )

    sys.excepthook = unhandled_exception

    store = None
    try:
        store = LocalStore()
        store.recover_interrupted()
        preferences = load_preferences(store)
        store.apply_retention(int(preferences["history_retention_days"]))
    except Exception:
        logging.getLogger(__name__).exception("Local application storage is unavailable.")
        if store is not None:
            try:
                store.close()
            except Exception:
                logging.getLogger(__name__).exception("Could not close unavailable local storage.")
        store = LocalStore(":memory:")
        preferences = load_preferences(store)
        QMessageBox.warning(
            None, "Local history unavailable",
            "ShareSpace could not access its local application data. The current session "
            "can continue, but settings and history will not persist after exit.",
        )

    platform_manager = create_platform_manager()
    window = MainWindow(platform_manager=platform_manager, store=store, preferences=preferences)
    window.show()

    application.aboutToQuit.connect(platform_manager.cleanup_on_exit)
    application.aboutToQuit.connect(store.close)
    return application.exec()
