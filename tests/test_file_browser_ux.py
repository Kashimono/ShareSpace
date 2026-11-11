from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from sharespace.core.models import (
    DirectoryListing, FileItem, FileItemType, SearchResults,
)
from sharespace.ui.file_browser_dialog import FileBrowserDialog


class _ClientStub:
    def close(self):
        pass


def _application():
    return QApplication.instance() or QApplication([])


def _item(name, path, *, kind=FileItemType.FILE, size=0, extension=""):
    return FileItem(
        filename=name, relative_path=path, extension=extension,
        size_bytes=size, modified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        item_type=kind,
    )


def test_file_browser_sorts_by_size_and_type_with_folders_first():
    _application()
    browser = FileBrowserDialog(_ClientStub())
    items = [
        _item("large.bin", "large.bin", size=30, extension=".bin"),
        _item("folder", "folder", kind=FileItemType.DIRECTORY),
        _item("small.txt", "small.txt", size=2, extension=".txt"),
    ]
    browser._sort_key = "size"
    assert [item.filename for item in browser._sort_items(items)] == [
        "folder", "small.txt", "large.bin"
    ]
    browser._sort_key = "type"
    assert [item.filename for item in browser._sort_items(items)] == [
        "folder", "large.bin", "small.txt"
    ]
    browser._sort_descending = True
    assert [item.filename for item in browser._sort_items(items)] == [
        "folder", "small.txt", "large.bin"
    ]
    browser.close()


def test_search_results_show_relative_location_and_open_containing_folder(monkeypatch):
    _application()
    browser = FileBrowserDialog(_ClientStub())
    browser._search_query = "résumé"
    result = _item("résumé final.pdf", "Documents/Reports/résumé final.pdf", size=4096, extension=".pdf")
    browser._search_finished(SearchResults(query="résumé", results=[result]), "résumé")
    assert browser._items.count() == 1
    assert "in /Documents/Reports" in browser._items.item(0).text()
    requested = []
    monkeypatch.setattr(browser, "_load_directory", lambda: requested.append(browser._path))
    browser._items.setCurrentRow(0)
    browser._open_search_result_location()
    assert requested == ["Documents/Reports"]
    assert browser._search_query == ""
    browser.close()


def test_search_results_refresh_only_when_live_event_matches(monkeypatch):
    _application()
    browser = FileBrowserDialog(_ClientStub())
    browser._search_query = "invoice"
    browser._search_results = [_item("invoice-old.pdf", "Archive/invoice-old.pdf", extension=".pdf")]
    requests = []
    monkeypatch.setattr(browser, "_queue_live_refresh", lambda: requests.append(True))
    browser.handle_session_event({
        "type": "file_created", "data": {
            "filename": "INVOICE March.pdf", "relative_path": "New/INVOICE March.pdf",
            "parent_path": "New",
        },
    })
    browser.handle_session_event({
        "type": "file_modified", "data": {
            "filename": "notes.txt", "relative_path": "Other/notes.txt", "parent_path": "Other",
        },
    })
    assert requests == [True]
    browser.close()


def test_breadcrumb_path_segments_navigate_to_parent(monkeypatch):
    _application()
    browser = FileBrowserDialog(_ClientStub())
    browser._path = "Documents/Reports/2026"
    browser._render_breadcrumbs()
    requested = []
    monkeypatch.setattr(browser, "_load_directory", lambda: requested.append(browser._path))
    browser._navigate_to("Documents/Reports")
    assert browser._path_label.text() == "Current folder: /Documents/Reports"
    assert requested == ["Documents/Reports"]
    browser.close()
