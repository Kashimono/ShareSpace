"""Qt application startup and process lifetime."""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QStandardPaths, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from sharespace import __version__
from sharespace.platform.platform_manager import create_platform_manager
from sharespace.storage.local_store import LocalStore
from sharespace.ui.app_settings import load_preferences
from sharespace.ui.main_window import MainWindow
from sharespace.ui.resources import resource_path


class _CredentialRedactionFilter(logging.Filter):
    """Remove credential-like values from persistent diagnostic logs."""

    _BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
    _ASSIGNMENT = re.compile(
        r"(?i)\b(joining code|join code|(?:host |participant )?token|"
        r"password|passphrase|credential)[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\r\n,;]+)"
    )

    @classmethod
    def _redact(cls, value: str) -> str:
        value = cls._BEARER.sub("Bearer [redacted]", value)
        return cls._ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[redacted]", value)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = self._redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def _configure_logging(data_directory: Path | None = None) -> Path | None:
    """Keep a small rotating diagnostic log in local application data."""
    if data_directory is None:
        location = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        data_directory = Path(location) if location else None
    if data_directory is None:
        logging.basicConfig(level=logging.INFO, handlers=[logging.NullHandler()], force=True)
        return None
    log_path = data_directory / "logs" / "sharespace.log"
    handler = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.addFilter(_CredentialRedactionFilter())
        if os.name != "nt":
            os.chmod(log_path, 0o600)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[handler],
            force=True,
        )
    except OSError:
        if handler is not None:
            handler.close()
        logging.basicConfig(level=logging.INFO, handlers=[logging.NullHandler()], force=True)
        return None
    return log_path


def main(argv: list[str] | None = None) -> int:
    """Create and run the ShareSpace desktop application."""
    arguments = list(sys.argv if argv is None else argv) or [sys.argv[0]]
    smoke_test = "--smoke-test" in arguments
    qt_arguments = [argument for argument in arguments if argument != "--smoke-test"]
    application = QApplication(qt_arguments)
    application.setApplicationName("ShareSpace")
    application.setOrganizationName("ShareSpace")
    application.setApplicationVersion(__version__)
    smoke_data_dir = None
    if smoke_test and os.environ.get("SHARESPACE_SMOKE_DATA_DIR"):
        smoke_data_dir = Path(os.environ["SHARESPACE_SMOKE_DATA_DIR"])
        smoke_data_dir.mkdir(parents=True, exist_ok=True)
    log_path = _configure_logging(smoke_data_dir)
    application.setWindowIcon(QIcon(str(resource_path("sharespace.png"))))

    def unhandled_exception(exception_type, exception, traceback) -> None:
        if issubclass(exception_type, KeyboardInterrupt):
            sys.__excepthook__(exception_type, exception, traceback)
            return
        logging.getLogger("sharespace").critical(
            "Unhandled application exception.", exc_info=(exception_type, exception, traceback)
        )
        if smoke_test:
            application.exit(1)
            return
        diagnostic_message = (
            "Diagnostic details were saved in the ShareSpace application log."
            if log_path is not None
            else "ShareSpace could not save a diagnostic log in local application data."
        )
        QMessageBox.critical(
            None, "ShareSpace error",
            "An unexpected error interrupted the application. Restart ShareSpace; "
            f"{diagnostic_message}",
        )

    sys.excepthook = unhandled_exception

    store = None
    try:
        store = LocalStore(smoke_data_dir / "sharespace.sqlite3" if smoke_data_dir else None)
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
        if smoke_test:
            raise
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

    def clean_platform_resources() -> None:
        logging.getLogger(__name__).info("Cleaning up platform resources.")
        platform_manager.cleanup_on_exit()
        logging.getLogger(__name__).info("Platform resource cleanup finished.")

    def close_local_store() -> None:
        logging.getLogger(__name__).info("Closing local application storage.")
        store.close()
        logging.getLogger(__name__).info("Local application storage closed.")

    application.aboutToQuit.connect(clean_platform_resources)
    application.aboutToQuit.connect(close_local_store)
    if smoke_test:
        logging.getLogger(__name__).info("Smoke test entering the Qt event loop.")

        def finish_smoke_test() -> None:
            logging.getLogger(__name__).info("Smoke test timer requested application shutdown.")
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            application.processEvents()
            application.quit()

        QTimer.singleShot(750, finish_smoke_test)
    exit_code = application.exec()
    if smoke_test:
        threads = [(thread.name, thread.daemon) for thread in threading.enumerate()]
        logging.getLogger(__name__).info(
            "Qt event loop exited with code %s; live Python threads: %s.", exit_code, threads
        )
    logging.shutdown()
    return exit_code
