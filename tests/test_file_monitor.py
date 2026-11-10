"""Windows/Linux filesystem integration checks for the confined watchdog worker."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from sharespace.core.file_manager import FileAccessError, SharedDirectoryManager
from sharespace.core.file_monitor import FileMonitor


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


@pytest.fixture
def running_monitor(tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    directories = SharedDirectoryManager()
    directories.select_root(root)
    lock = threading.Lock()
    events: list[dict[str, object]] = []
    states: list[bool] = []

    def on_event(_generation: int, event: dict[str, object]) -> None:
        with lock:
            events.append(event)

    def on_state(_generation: int, available: bool) -> None:
        with lock:
            states.append(available)

    monitor = FileMonitor(directories, on_event, on_state, debounce_seconds=0.1)
    generation = monitor.set_root(root)

    def snapshot():
        with lock:
            return list(events), list(states)

    try:
        yield root, directories, monitor, generation, snapshot
    finally:
        monitor.stop()


def test_detect_create_modify_delete_and_nested_files(running_monitor):
    root, _manager, _monitor, _generation, snapshot = running_monitor
    file_path = root / "first.txt"
    file_path.write_text("first", encoding="utf-8")
    assert _wait_for(lambda: any(
        event.get("type") == "file_created" and event.get("relative_path") == "first.txt"
        for event in snapshot()[0]
    ))

    file_path.write_text("modified contents", encoding="utf-8")
    assert _wait_for(lambda: any(
        event.get("type") == "file_modified" and event.get("relative_path") == "first.txt"
        for event in snapshot()[0]
    ))
    events, _states = snapshot()
    assert sum(event.get("type") == "file_created" and event.get("relative_path") == "first.txt" for event in events) == 1
    assert sum(event.get("type") == "file_modified" and event.get("relative_path") == "first.txt" for event in events) == 1

    nested = root / "nested"
    nested.mkdir()
    child = nested / "child.txt"
    child.write_text("nested", encoding="utf-8")
    assert _wait_for(lambda: any(
        event.get("type") == "file_created" and event.get("relative_path") == "nested/child.txt"
        for event in snapshot()[0]
    ))
    assert _wait_for(lambda: any(
        event.get("type") == "shared_directory_changed"
        and event.get("relative_path") == "nested"
        and event.get("change") == "created"
        for event in snapshot()[0]
    ))

    child.unlink()
    assert _wait_for(lambda: any(
        event.get("type") == "file_deleted" and event.get("relative_path") == "nested/child.txt"
        for event in snapshot()[0]
    ))


def test_move_is_reported_as_delete_and_create(running_monitor):
    root, _manager, _monitor, _generation, snapshot = running_monitor
    source = root / "source.txt"
    destination = root / "destination.txt"
    source.write_text("move me", encoding="utf-8")
    assert _wait_for(lambda: any(
        event.get("type") == "file_created" and event.get("relative_path") == "source.txt"
        for event in snapshot()[0]
    ))
    source.rename(destination)
    assert _wait_for(lambda: (
        any(event.get("type") == "file_deleted" and event.get("relative_path") == "source.txt" for event in snapshot()[0])
        and any(event.get("type") == "file_created" and event.get("relative_path") == "destination.txt" for event in snapshot()[0])
    ))


def test_ignores_hidden_symlink_and_known_temporary_files(running_monitor):
    root, _manager, _monitor, _generation, snapshot = running_monitor
    (root / ".hidden.txt").write_text("private", encoding="utf-8")
    (root / ".sharespace-abc.part").write_text("partial", encoding="utf-8")
    (root / "~$draft.docx").write_text("temp", encoding="utf-8")
    (root / "partial.tmp").write_text("temp", encoding="utf-8")
    outside = root.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (root / "linked.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pass
    assert _wait_for(lambda: _monitor._event_queue is not None and _monitor._event_queue.empty())
    time.sleep(0.2)
    assert snapshot()[0] == []
    assert not _monitor._valid_public_relative_path("../outside.txt")
    assert not _monitor._valid_public_relative_path(".hidden.txt")


def test_root_change_stops_old_watches_and_starts_new_root(running_monitor, tmp_path):
    old_root, directories, monitor, generation, snapshot = running_monitor
    new_root = tmp_path / "other-shared"
    new_root.mkdir()
    directories.select_root(new_root)
    new_generation = monitor.set_root(new_root)
    assert new_generation > generation
    old_events, _ = snapshot()
    old_root.joinpath("stale.txt").write_text("old", encoding="utf-8")
    new_root.joinpath("current.txt").write_text("new", encoding="utf-8")
    assert _wait_for(lambda: any(
        event.get("type") == "file_created" and event.get("relative_path") == "current.txt"
        for event in snapshot()[0]
    ))
    time.sleep(0.25)
    latest = snapshot()[0]
    assert not any(event.get("relative_path") == "stale.txt" for event in latest[len(old_events):])


def test_stop_releases_observer_and_drops_later_changes(running_monitor):
    root, _manager, monitor, _generation, snapshot = running_monitor
    monitor.set_root(None)
    assert monitor._observer is None
    assert monitor._worker is None
    (root / "after-stop.txt").write_text("ignored", encoding="utf-8")
    time.sleep(0.2)
    assert not any(event.get("relative_path") == "after-stop.txt" for event in snapshot()[0])


def test_deleted_root_reports_unavailable_then_recovers(running_monitor):
    root, _manager, _monitor, _generation, snapshot = running_monitor
    detached = root.with_name("detached-shared")
    root.rename(detached)
    assert _wait_for(lambda: False in snapshot()[1])
    root.mkdir()
    assert _wait_for(lambda: True in snapshot()[1])
    assert _wait_for(lambda: any(
        event.get("type") == "shared_directory_changed" and event.get("available") is True
        for event in snapshot()[0]
    ))


def test_inaccessible_subtree_does_not_emit_false_deletions(running_monitor, monkeypatch):
    root, directories, _monitor, _generation, snapshot = running_monitor
    private = root / "restricted"
    private.mkdir()
    secret = private / "known.txt"
    secret.write_text("known", encoding="utf-8")
    assert _wait_for(lambda: any(
        event.get("type") == "file_created" and event.get("relative_path") == "restricted/known.txt"
        for event in snapshot()[0]
    ))
    # Use a listing fault to model platforms that report an inaccessible folder.
    original_listing = directories.listing

    def listing(path=""):
        if path == "restricted":
            raise FileAccessError("directory unavailable", "directory_unavailable", 403)
        return original_listing(path)

    monkeypatch.setattr(directories, "listing", listing)
    (root / "trigger.txt").write_text("wake", encoding="utf-8")
    assert _wait_for(lambda: any(
        event.get("type") == "file_created" and event.get("relative_path") == "trigger.txt"
        for event in snapshot()[0]
    ))
    assert not any(
        event.get("type") == "file_deleted" and event.get("relative_path") == "restricted/known.txt"
        for event in snapshot()[0]
    )
