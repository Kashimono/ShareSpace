from __future__ import annotations

from sharespace.core.models import TransferDirection, TransferStatus
from sharespace.core.transfer_manager import TransferManager
from sharespace.storage.local_store import LocalStore


def test_settings_and_terminal_transfer_history_survive_restart(tmp_path):
    path = tmp_path / "app-data" / "sharespace.sqlite3"
    store = LocalStore(path)
    store.set_setting("theme", "dark")
    transfers = TransferManager(on_change=store.save_transfer)
    transfer = transfers.create(
        filename="résumé (final).pdf",
        direction=TransferDirection.DOWNLOAD,
        total=4,
        participant_id="local",
        transfer_id="a" * 32,
    )
    transfers.begin(transfer.transfer_id)
    transfers.advance(transfer.transfer_id, 4)
    transfers.complete(transfer.transfer_id)
    store.close()

    reopened = LocalStore(path)
    assert reopened.get_setting("theme") == "dark"
    records = reopened.transfer_records()
    assert len(records) == 1
    assert records[0]["filename"] == "résumé (final).pdf"
    assert records[0]["direction"] == "DOWNLOAD"
    assert records[0]["status"] == TransferStatus.COMPLETED.value
    assert records[0]["transferred_bytes"] == 4
    reopened.close()


def test_active_transfers_and_sessions_become_interrupted_on_restart(tmp_path):
    path = tmp_path / "sharespace.sqlite3"
    store = LocalStore(path)
    transfers = TransferManager(on_change=store.save_transfer)
    transfers.create(
        filename="unfinished.bin", direction=TransferDirection.UPLOAD,
        total=100, participant_id="peer-id", transfer_id="b" * 32,
    )
    session_id = store.start_session(
        session_id="c" * 32, session_name="Local room", host_device_name="Host PC",
        role="HOST", participant_count=1,
    )
    store.close()

    reopened = LocalStore(path)
    assert reopened.recover_interrupted() == (1, 1)
    transfer = reopened.transfer_records()[0]
    assert transfer["status"] == "INTERRUPTED"
    assert transfer["error_summary"]
    session = reopened.session_records()[0]
    assert session["record_id"] == session_id
    assert session["outcome"] == "INTERRUPTED"
    assert session["ended_at"]
    reopened.close()


def test_history_filter_sort_clear_and_active_state_protection(tmp_path):
    store = LocalStore(tmp_path / "sharespace.sqlite3")
    manager = TransferManager(on_change=store.save_transfer)
    for transfer_id, filename, direction in (
        ("d" * 32, "zeta.txt", TransferDirection.UPLOAD),
        ("e" * 32, "alpha.txt", TransferDirection.DOWNLOAD),
    ):
        item = manager.create(
            filename=filename, direction=direction, total=0,
            participant_id="peer", transfer_id=transfer_id,
        )
        manager.complete(item.transfer_id)
    active = manager.create(
        filename="active.txt", direction=TransferDirection.UPLOAD, total=2,
        participant_id="peer", transfer_id="f" * 32,
    )
    assert [row["filename"] for row in store.transfer_records(sort_by="filename", descending=False)] == [
        "active.txt", "alpha.txt", "zeta.txt"
    ]
    assert [row["direction"] for row in store.transfer_records(direction="DOWNLOAD")] == ["DOWNLOAD"]
    assert store.clear_transfer_history() == 2
    assert [row["transfer_id"] for row in store.transfer_records()] == [active.transfer_id]

    record_id = store.start_session(
        session_id="1" * 32, session_name="Joined", host_device_name="Host", role="PARTICIPANT"
    )
    assert store.session_records() == []
    store.finish_session(record_id, "DISCONNECTED")
    assert store.session_records(role="PARTICIPANT")[0]["outcome"] == "DISCONNECTED"
    assert store.clear_session_history() == 1
    assert store.session_records() == []
    store.close()


def test_sensitive_error_paths_are_not_persisted(tmp_path):
    store = LocalStore(tmp_path / "sharespace.sqlite3")
    manager = TransferManager(on_change=store.save_transfer)
    transfer = manager.create(
        filename="private.txt", direction=TransferDirection.DOWNLOAD,
        total=0, participant_id="local", transfer_id="9" * 32,
    )
    manager.fail(transfer.transfer_id, r"Could not open C:\Users\aronr\Private\secret.txt")
    record = store.transfer_records()[0]
    assert "C:\\Users" not in record["error_summary"]
    assert "secret.txt" not in record["error_summary"]
    store.close()


def test_failed_cancelled_and_failed_session_records_are_kept(tmp_path):
    store = LocalStore(tmp_path / "sharespace.sqlite3")
    manager = TransferManager(on_change=store.save_transfer)
    failed = manager.create(
        filename="failed.dat", direction=TransferDirection.UPLOAD,
        total=1, participant_id="peer", transfer_id="2" * 32,
    )
    manager.fail(failed.transfer_id, "The host rejected the upload.")
    cancelled = manager.create(
        filename="cancelled.dat", direction=TransferDirection.DOWNLOAD,
        total=1, participant_id="local", transfer_id="3" * 32,
    )
    manager.cancel(cancelled.transfer_id)
    store.record_failed_session(
        session_id="4" * 32, session_name="Join attempt", host_device_name="Host",
        role="PARTICIPANT",
    )
    records = store.transfer_records()
    assert {row["status"] for row in records} == {"FAILED", "CANCELLED"}
    assert store.session_records(role="PARTICIPANT")[0]["outcome"] == "FAILED"
    store.close()
