"""Validation, targeting, and backpressure checks for v0.6 WSS events."""

import asyncio

import pytest

from sharespace.networking.websocket_events import (
    EventBroker,
    RecentEventIds,
    SessionEvent,
    SessionEventType,
    normalize_legacy_event,
    validate_wire_event,
)


def test_file_event_envelope_contains_safe_relative_path_and_stable_identity():
    event = normalize_legacy_event({
        "type": "file_created",
        "filename": "Report.pdf",
        "relative_path": "Docs/Report.pdf",
        "parent_path": "Docs",
        "size_bytes": 12,
    })
    message = event.to_message()

    assert message["type"] == "file_created"
    assert message["event_type"] == "FILE_CREATED"
    assert len(message["event_id"]) == 32
    assert message["data"]["relative_path"] == "Docs/Report.pdf"
    assert "absolute_path" not in message["data"]
    assert validate_wire_event(message).event_id == event.event_id


@pytest.mark.parametrize("relative_path", [
    "C:/private/secret.txt",
    "/private/secret.txt",
    "../secret.txt",
    "Docs/../secret.txt",
    ".private/secret.txt",
    "Docs/.secret.txt",
    "Docs/.sharespace-upload.part",
    "Docs/incomplete.tmp",
    "Docs\\secret.txt",
])
def test_file_event_rejects_unsafe_or_private_paths(relative_path):
    filename = relative_path.replace("\\", "/").rsplit("/", 1)[-1]
    parent = relative_path.replace("\\", "/").rpartition("/")[0]
    with pytest.raises(ValueError):
        SessionEvent(
            event_type=SessionEventType.FILE_CREATED,
            data={"filename": filename, "relative_path": relative_path, "parent_path": parent},
        )


def test_wire_event_rejects_mismatched_legacy_alias_and_naive_timestamp():
    valid = normalize_legacy_event({
        "type": "session_resync_required",
    }).to_message()
    invalid_alias = {**valid, "type": "file_deleted"}
    with pytest.raises(ValueError):
        validate_wire_event(invalid_alias)
    with pytest.raises(ValueError):
        SessionEvent(
            event_type=SessionEventType.SESSION_RESYNC_REQUIRED,
            timestamp="2026-01-01T00:00:00",
            data={},
        )


def test_broker_targets_permission_events_and_excludes_selected_participant():
    async def exercise():
        broker = EventBroker()
        first = broker.subscribe()
        second = broker.subscribe()
        broker.bind_participant(first, "a" * 32)
        broker.bind_participant(second, "b" * 32)
        await broker.publish(
            {"type": "participant_permission_changed", "participant_id": "a" * 32, "permission": "READ_WRITE"},
            target_participant_id="a" * 32,
        )
        assert first.get_nowait()["permission"] == "READ_WRITE"
        assert second.empty()
        await broker.publish(
            {"type": "session_resync_required"},
            exclude_participant_id="a" * 32,
        )
        assert first.empty()
        assert second.get_nowait()["event_type"] == "SESSION_RESYNC_REQUIRED"

    asyncio.run(exercise())


def test_broker_backpressure_replaces_stale_queue_with_resync_hint():
    async def exercise():
        broker = EventBroker(queue_size=2)
        queue = broker.subscribe()
        for _ in range(4):
            await broker.publish({"type": "session_resync_required"})
        events = [queue.get_nowait(), queue.get_nowait()]
        assert all(item["event_type"] == "SESSION_RESYNC_REQUIRED" for item in events)
        assert all(len(item["event_id"]) == 32 for item in events)

    asyncio.run(exercise())


def test_broker_drops_invalid_outgoing_events_without_disrupting_other_events():
    async def exercise():
        broker = EventBroker()
        queue = broker.subscribe()
        await broker.publish({"type": "file_created", "relative_path": "../secrets", "filename": "secrets", "parent_path": ".."})
        assert queue.empty()
        await broker.publish({"type": "session_resync_required"})
        assert queue.get_nowait()["event_type"] == "SESSION_RESYNC_REQUIRED"

    asyncio.run(exercise())


def test_recent_event_ids_suppress_duplicates_with_bounded_memory():
    window = RecentEventIds(capacity=2)
    assert window.add("first") is True
    assert window.add("second") is True
    assert window.add("first") is False
    assert window.add("third") is True
    assert window.add("first") is True
