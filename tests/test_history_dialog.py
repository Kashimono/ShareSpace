from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from sharespace.core.models import TransferDirection
from sharespace.core.transfer_manager import TransferManager
from sharespace.storage.local_store import LocalStore
from sharespace.ui.history_dialog import HistoryDialog


def _application():
    return QApplication.instance() or QApplication([])


def test_history_dialog_filters_and_presents_transfer_rows(tmp_path, monkeypatch):
    _application()
    store = LocalStore(tmp_path / "sharespace.sqlite3")
    transfers = TransferManager(on_change=store.save_transfer)
    for identifier, filename, direction in (
        ("a" * 32, "report.pdf", TransferDirection.DOWNLOAD),
        ("b" * 32, "draft.txt", TransferDirection.UPLOAD),
    ):
        transfer = transfers.create(
            filename=filename, direction=direction, total=0,
            participant_id="peer", transfer_id=identifier,
        )
        transfers.complete(transfer.transfer_id)
    dialog = HistoryDialog(store, "transfers")
    assert dialog._rows.count() == 2
    assert "report.pdf" in dialog._rows.item(0).text()
    dialog._filter.setCurrentIndex(dialog._filter.findData("DOWNLOAD"))
    assert dialog._rows.count() == 1
    assert "report.pdf" in dialog._rows.item(0).text()
    dialog.close()
    store.close()
