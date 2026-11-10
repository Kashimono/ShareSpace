from datetime import datetime, timedelta, timezone

import pytest

from sharespace.core.models import (
    JoinRequestStatus,
    ParticipantRole,
    ParticipantStatus,
    PermissionLevel,
    SessionStatus,
)
from sharespace.core.session_manager import (
    InvalidCredential,
    InvalidTransition,
    PermissionDenied,
    SessionManager,
)


def create_session(manager=None, *, now=None):
    manager = manager or SessionManager()
    session, credentials = manager.create_session(
        session_name="Test room",
        host_device_name="Host device",
        host_os="Windows",
        host_ip="192.168.1.20",
        port=54832,
        now=now,
    )
    return manager, session, credentials


def request_join(manager, session, credentials, *, client_id="client-1", now=None):
    return manager.request_join(
        session_id=session.session_id,
        client_id=client_id,
        device_name="Peer device",
        operating_system="Linux",
        joining_code=credentials.joining_code,
        source_address="192.168.1.21",
        now=now,
    )


def test_session_creation_generates_unique_id_and_high_entropy_join_code():
    first_manager, first, first_credentials = create_session()
    second_manager, second, second_credentials = create_session()

    assert first.session_id != second.session_id
    assert len(first_credentials.joining_code) == 12
    assert first_credentials.joining_code != second_credentials.joining_code
    assert first.status is SessionStatus.ACTIVE
    assert first.participant_count == 1
    _, _, host = first_manager.host_snapshot(first_credentials.host_token)
    assert host[0].role is ParticipantRole.HOST
    assert host[0].status is ParticipantStatus.CONNECTED


def test_join_request_is_idempotent_per_client_and_code_is_not_exposed_in_snapshot():
    manager, session, credentials = create_session()
    request_id, request_token, status = request_join(manager, session, credentials)
    repeated_id, repeated_token, repeated_status = request_join(manager, session, credentials)

    assert (request_id, status) == (repeated_id, repeated_status)
    assert request_token != repeated_token
    assert status is JoinRequestStatus.PENDING
    _, pending, participants = manager.host_snapshot(credentials.host_token)
    assert len(pending) == 1
    assert len([item for item in participants if item.role is ParticipantRole.PARTICIPANT]) == 1
    assert credentials.joining_code not in repr((pending, participants))


def test_invalid_joining_code_is_rejected_and_does_not_create_a_participant():
    manager, session, _credentials = create_session()

    with pytest.raises(InvalidCredential):
        manager.request_join(
            session_id=session.session_id,
            client_id="client-1",
            device_name="Peer",
            operating_system="Linux",
            joining_code="BADCODEBADCODE",
            source_address="192.168.1.21",
        )

    assert manager.get_public_session().participant_count == 1


def test_approval_registers_participant_and_host_can_disconnect_them():
    manager, session, credentials = create_session()
    request_id, request_token, _ = request_join(manager, session, credentials)
    manager.approve_request(request_id, credentials.host_token)

    status, participant_token = manager.poll_join_request(request_id, request_token)
    assert status is JoinRequestStatus.APPROVED
    assert participant_token
    participant = manager.authenticate_participant(participant_token)
    assert participant.status is ParticipantStatus.CONNECTED
    assert participant.role is ParticipantRole.PARTICIPANT
    assert manager.get_public_session().participant_count == 2


def test_approved_participants_default_read_only_and_only_host_can_change_permission():
    manager, session, credentials = create_session()
    request_id, request_token, _ = request_join(manager, session, credentials)
    manager.approve_request(request_id, credentials.host_token)
    _, participant_token = manager.poll_join_request(request_id, request_token)
    participant = manager.authenticate_participant(participant_token)

    assert participant.permission is PermissionLevel.READ_ONLY
    with pytest.raises(PermissionDenied):
        manager.set_participant_permission(
            participant.participant_id, PermissionLevel.READ_WRITE, participant_token
        )

    changed = manager.set_participant_permission(
        participant.participant_id, PermissionLevel.READ_WRITE, credentials.host_token
    )
    assert changed.permission is PermissionLevel.READ_WRITE
    with pytest.raises(PermissionDenied):
        manager.set_participant_permission(
            participant.participant_id, PermissionLevel.READ_ONLY, "invalid-host-token"
        )

    disconnected = manager.disconnect_participant(participant.participant_id, credentials.host_token)
    assert disconnected.status is ParticipantStatus.DISCONNECTED
    assert manager.get_public_session().participant_count == 1
    with pytest.raises(InvalidCredential):
        manager.authenticate_participant(participant_token)


def test_rejection_and_unauthorized_host_operations_are_enforced():
    manager, session, credentials = create_session()
    request_id, request_token, _ = request_join(manager, session, credentials)

    with pytest.raises(PermissionDenied):
        manager.approve_request(request_id, "not-the-host")
    manager.reject_request(request_id, credentials.host_token)
    status, participant_token = manager.poll_join_request(request_id, request_token)

    assert status is JoinRequestStatus.REJECTED
    assert participant_token is None
    with pytest.raises(InvalidTransition):
        manager.approve_request(request_id, credentials.host_token)


def test_temporary_interruptions_can_reconnect_but_stale_heartbeats_expire():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    manager = SessionManager(heartbeat_timeout_seconds=5)
    manager, session, credentials = create_session(manager, now=now)
    request_id, request_token, _ = request_join(manager, session, credentials, now=now)
    manager.approve_request(request_id, credentials.host_token)
    _, participant_token = manager.poll_join_request(request_id, request_token)
    first_connection = manager.authenticate_participant(participant_token, now=now)
    manager.participant_disconnected(first_connection.participant_id)
    resumed = manager.authenticate_participant(participant_token, now=now + timedelta(seconds=3))

    assert resumed.participant_id == first_connection.participant_id
    assert resumed.status is ParticipantStatus.CONNECTED
    changes = manager.expire_inactive(now=now + timedelta(seconds=9))
    assert "participant_disconnected" in changes
    with pytest.raises(InvalidCredential):
        manager.authenticate_participant(participant_token, now=now + timedelta(seconds=10))


def test_session_termination_requires_host_and_revokes_active_state():
    manager, _session, credentials = create_session()
    with pytest.raises(PermissionDenied):
        manager.end_session("bad-token")

    ended = manager.end_session(credentials.host_token)
    assert ended.status is SessionStatus.ENDED
    assert manager.active_session() is False
    with pytest.raises(PermissionDenied):
        manager.host_snapshot(credentials.host_token)
