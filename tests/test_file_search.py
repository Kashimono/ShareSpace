from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sharespace.core.file_manager import FileAccessError, SharedDirectoryManager
from sharespace.core.models import FileItemType
from sharespace.core.session_manager import SessionManager
from sharespace.networking.session_server import create_session_app


def test_tree_search_matches_unicode_case_insensitive_names_and_folders(tmp_path):
    (tmp_path / "Quarterly Reports").mkdir()
    nested = tmp_path / "Documents" / "Nested Folder"
    nested.mkdir(parents=True)
    (tmp_path / "Résumé @ 2026.txt").write_text("name only", encoding="utf-8")
    (nested / "résumé final (copy).TXT").write_text("not read", encoding="utf-8")
    (tmp_path / ".hidden-résumé.txt").write_text("hidden", encoding="utf-8")
    (nested / ".sharespace-upload.part").write_bytes(b"temporary")

    manager = SharedDirectoryManager()
    manager.select_root(tmp_path)

    exact = manager.search("RÉSUMÉ")
    assert {item.filename for item in exact.results} == {
        "Résumé @ 2026.txt", "résumé final (copy).TXT"
    }
    nested_result = next(item for item in exact.results if item.filename.startswith("résumé final"))
    assert nested_result.relative_path == "Documents/Nested Folder/résumé final (copy).TXT"
    assert nested_result.size_bytes == len("not read")
    folders = manager.search("quarter")
    assert len(folders.results) == 1
    assert folders.results[0].item_type is FileItemType.DIRECTORY
    assert not manager.search(".sharespace").results
    assert not manager.search("does not exist").results


def test_tree_search_is_bounded_and_rejects_invalid_input(tmp_path):
    for index in range(7):
        (tmp_path / f"match-{index}.txt").write_text("x")
    manager = SharedDirectoryManager()
    manager.select_root(tmp_path)
    results = manager.search("MATCH", limit=3)
    assert len(results.results) == 3
    assert results.truncated is True
    with pytest.raises(FileAccessError):
        manager.search("\x00")
    with pytest.raises(FileAccessError):
        manager.search("   ")
    with pytest.raises(FileAccessError):
        manager.search("x" * 129)
    with pytest.raises(FileAccessError):
        manager.search("match", limit=501)


def test_tree_search_excludes_links_and_unavailable_shared_directory(tmp_path):
    outside = tmp_path.parent / "outside-search-secret.txt"
    outside.write_text("private", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "outside-secret-link.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable on this platform.")
    manager = SharedDirectoryManager()
    manager.select_root(root)
    assert not manager.search("outside").results
    manager.stop_sharing()
    with pytest.raises(FileAccessError, match="not sharing"):
        manager.search("anything")


def test_search_route_requires_connected_participant_and_returns_only_relative_metadata(tmp_path):
    (tmp_path / "Nested").mkdir()
    (tmp_path / "Nested" / "report (résumé).pdf").write_bytes(b"pdf metadata only")
    manager = SessionManager()
    session, credentials = manager.create_session(
        session_name="Search room", host_device_name="Host", host_os="Windows",
        host_ip="192.168.1.20", port=55001,
    )
    request_id, request_token, _status = manager.request_join(
        session_id=session.session_id, client_id="search-peer", device_name="Peer",
        operating_system="Linux", joining_code=credentials.joining_code,
        source_address="192.168.1.21",
    )
    manager.approve_request(request_id, credentials.host_token)
    _status, participant_token = manager.poll_join_request(request_id, request_token)
    manager.authenticate_participant(participant_token)
    app = create_session_app(manager)
    app.state.file_service.select_root(tmp_path)

    with TestClient(app) as client:
        assert client.get("/api/v1/files/search", params={"q": "report"}).status_code == 401
        assert client.get(
            "/api/v1/files/search", params={"q": "report"},
            headers={"Authorization": f"Bearer {credentials.host_token}"},
        ).status_code == 401
        response = client.get(
            "/api/v1/files/search", params={"q": "RÉSUMÉ"},
            headers={"Authorization": f"Bearer {participant_token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["results"][0]["filename"] == "report (résumé).pdf"
        assert body["results"][0]["relative_path"] == "Nested/report (résumé).pdf"
        assert str(tmp_path) not in response.text
        assert client.get(
            "/api/v1/files/search", params={"q": "../"},
            headers={"Authorization": f"Bearer {participant_token}"},
        ).json()["results"] == []
        assert client.get(
            "/api/v1/files/search", params={"q": "r", "limit": "501"},
            headers={"Authorization": f"Bearer {participant_token}"},
        ).status_code == 422
