"""Small Qt workers for synchronous setup and asynchronous session calls."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Signal

logger = logging.getLogger(__name__)


class SessionWorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)
    event = Signal(object)
    connection_state = Signal(str)
    progress = Signal(object)


class SessionWorker(QRunnable):
    """Run blocking setup or one async operation away from the Qt thread."""

    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self.operation = operation
        self.signals = SessionWorkerSignals()

    def run(self) -> None:
        try:
            result = self.operation()
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            self.signals.finished.emit(result)
        except Exception as error:
            logger.exception("Session operation failed.")
            self.signals.failed.emit(str(error))


class SessionEventWorker(QRunnable):
    """Keep a WebSocket event stream and participant heartbeat off the UI thread."""

    def __init__(self, operation: Callable[[SessionWorkerSignals], object]) -> None:
        super().__init__()
        self.operation = operation
        self.signals = SessionWorkerSignals()

    def run(self) -> None:
        try:
            result = self.operation(self.signals)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            self.signals.finished.emit(result)
        except Exception as error:
            logger.exception("Session event stream failed.")
            self.signals.failed.emit(str(error))
