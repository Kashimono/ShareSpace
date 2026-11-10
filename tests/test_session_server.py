import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sharespace.core.session_manager import SessionManager
from sharespace.networking.session_server import create_session_app


def make_session(host_os="Windows"):
    manager = SessionManager()
    session, credentials = manager.create_session(
        session_name="API room",
        host_device_name="Host PC",
        host_os=host_os,
        host_ip="192.168.1.25",
        port=54832,
    )
    return manager, session, credentials, create_session_app(manager)


def submit_join(client, session_id, code, *, client_id="peer-1", os_name="Linux"):
    return client.post(
        f"/api/v1/sessions/{session_id}/join-requests",
        json={
            "client_id": client_id,
            "device_name": "Peer laptop",
            "operating_system": os_name,
            "joining_code": code,
        },
    )


def test_public_session_does_not_disclose_joining_code_or_credentials():
    _manager, session, credentials, app = make_session()
    with TestClient(app) as client:
        response = client.get("/api/v1/session")

    assert response.status_code == 200
    assert response.json()["session_id"] == session.session_id
    assert credentials.joining_code not in response.text
    assert credentials.host_token not in response.text


def test_join_code_authentication_approval_and_participant_registration():
    manager, session, credentials, app = make_session()
    with TestClient(app) as client:
        created = submit_join(client, session.session_id, credentials.joining_code)
        assert created.status_code == 200
        request = created.json()
        assert request["status"] == "pending"

        pending = client.get(
            f"/api/v1/host/snapshot",
            headers={"Authorization": f"Bearer {credentials.host_token}"},
        )
        assert len(pending.json()["pending_requests"]) == 1
        request_id = pending.json()["pending_requests"][0]["request_id"]
        approved = client.post(
            f"/api/v1/host/join-requests/{request_id}/approve",
            headers={"Authorization": f"Bearer {credentials.host_token}"},
        )
        assert approved.status_code == 200
        poll = client.get(
            f"/api/v1/join-requests/{request_id}",
            headers={"Authorization": f"Bearer {request['request_token']}"},
        )
        participant_token = poll.json()["participant_token"]
        assert poll.json()["status"] == "approved"

        with client.websocket_connect(f"/api/v1/sessions/{session.session_id}/events") as websocket:
            websocket.send_json({"type": "AUTHENTICATE", "token": participant_token})
            authenticated = websocket.receive_json()
            assert authenticated["type"] == "authenticated"
            assert authenticated["role"] == "PARTICIPANT"
            websocket.send_json({"type": "HEARTBEAT"})
            assert websocket.receive_json()["type"] == "heartbeat_ack"

        snapshot = manager.host_snapshot(credentials.host_token)
        peer = next(item for item in snapshot[2] if item.role.value == "PARTICIPANT")
        assert peer.status.value == "DISCONNECTED"


def test_invalid_code_validation_errors_do_not_echo_submitted_values():
    _manager, session, _credentials, app = make_session()
    with TestClient(app) as client:
        invalid_auth = submit_join(client, session.session_id, "NOTTHECODE123")
        invalid_shape = client.post(
            f"/api/v1/sessions/{session.session_id}/join-requests",
            json={
                "client_id": "peer-2",
                "device_name": "Peer laptop",
                "operating_system": "Linux",
                "joining_code": "SHORT",
            },
        )

    assert invalid_auth.status_code == 401
    assert invalid_shape.status_code == 422
    assert "SHORT" not in invalid_shape.text


def test_host_only_operations_reject_missing_and_invalid_credentials():
    _manager, session, _credentials, app = make_session()
    with TestClient(app) as client:
        no_auth = client.get("/api/v1/host/snapshot")
        bad_auth = client.post(
            "/api/v1/host/end",
            headers={"Authorization": "Bearer not-the-host"},
        )

    assert no_auth.status_code == 403
    assert bad_auth.status_code == 403
    assert bad_auth.json()["code"] == "permission_denied"
    assert session.status.value == "active"


@pytest.mark.parametrize("host_os,peer_os", [("Windows", "Linux"), ("Linux", "Windows")])
def test_protocol_accepts_mixed_platform_metadata(host_os, peer_os):
    _manager, session, credentials, app = make_session(host_os=host_os)
    with TestClient(app) as client:
        response = submit_join(client, session.session_id, credentials.joining_code, os_name=peer_os)
        snapshot = client.get(
            "/api/v1/host/snapshot",
            headers={"Authorization": f"Bearer {credentials.host_token}"},
        )

    assert response.status_code == 200
    assert snapshot.json()["session"]["host_os"] == host_os
    assert snapshot.json()["pending_requests"][0]["operating_system"] == peer_os


def test_session_end_is_host_only_and_notifies_connected_websocket_clients():
    _manager, session, credentials, app = make_session()
    with TestClient(app) as client:
        created = submit_join(client, session.session_id, credentials.joining_code)
        request_data = created.json()
        request_id = request_data["request_id"]
        client.post(
            f"/api/v1/host/join-requests/{request_id}/approve",
            headers={"Authorization": f"Bearer {credentials.host_token}"},
        )
        poll = client.get(
            f"/api/v1/join-requests/{request_id}",
            headers={"Authorization": f"Bearer {request_data['request_token']}"},
        )
        participant_token = poll.json()["participant_token"]
        with client.websocket_connect(f"/api/v1/sessions/{session.session_id}/events") as websocket:
            websocket.send_json({"type": "AUTHENTICATE", "token": participant_token})
            assert websocket.receive_json()["type"] == "authenticated"
            assert websocket.receive_json()["type"] == "participant_connected"
            ended = client.post(
                "/api/v1/host/end",
                headers={"Authorization": f"Bearer {credentials.host_token}"},
            )
            event = websocket.receive_json()

    assert ended.status_code == 200
    assert ended.json()["status"] == "ended"
    assert event["type"] == "session_ended"


def test_host_disconnect_closes_the_participant_socket_and_rejects_duplicate_authentication():
    manager, session, credentials, app = make_session()
    with TestClient(app) as client:
        created = submit_join(client, session.session_id, credentials.joining_code)
        request_data = created.json()
        request_id = request_data["request_id"]
        client.post(
            f"/api/v1/host/join-requests/{request_id}/approve",
            headers={"Authorization": f"Bearer {credentials.host_token}"},
        )
        poll = client.get(
            f"/api/v1/join-requests/{request_id}",
            headers={"Authorization": f"Bearer {request_data['request_token']}"},
        )
        participant_token = poll.json()["participant_token"]
        authorization = {"Authorization": f"Bearer {credentials.host_token}"}
        with client.websocket_connect(f"/api/v1/sessions/{session.session_id}/events") as host_socket:
            host_socket.send_json({"type": "AUTHENTICATE", "token": credentials.host_token})
            assert host_socket.receive_json()["role"] == "HOST"
            with client.websocket_connect(f"/api/v1/sessions/{session.session_id}/events") as first:
                first.send_json({"type": "AUTHENTICATE", "token": participant_token})
                authenticated = first.receive_json()
                participant_id = authenticated["participant_id"]
                joined = host_socket.receive_json()
                assert joined["type"] == "participant_connected"
                assert joined["event_type"] == "PARTICIPANT_JOINED"
                assert first.receive_json()["event_type"] == "PARTICIPANT_JOINED"

                with client.websocket_connect(f"/api/v1/sessions/{session.session_id}/events") as duplicate:
                    duplicate.send_json({"type": "AUTHENTICATE", "token": participant_token})
                    with pytest.raises(WebSocketDisconnect):
                        duplicate.receive_json()

                disconnected = client.post(
                    f"/api/v1/host/participants/{participant_id}/disconnect",
                    headers=authorization,
                )
                assert disconnected.status_code == 200
                terminal = first.receive_json()
                assert terminal == {"type": "error", "code": "participant_disconnected"}
                left = host_socket.receive_json()
                assert left["type"] == "participant_disconnected"
                assert left["event_type"] == "PARTICIPANT_LEFT"
                assert left["data"]["participant_id"] == participant_id

    snapshot = manager.host_snapshot(credentials.host_token)
    peers = [item for item in snapshot[2] if item.role.value == "PARTICIPANT"]
    assert len(peers) == 1
    assert peers[0].status.value == "DISCONNECTED"
