from __future__ import annotations

import os
import io
import logging
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from sharespace import __version__
from sharespace.app import _CredentialRedactionFilter
from sharespace.ui.resources import resource_path


def test_version_and_source_resource_path():
    assert __version__ == "1.0.0"
    asset = resource_path("sharespace.png")
    assert asset.is_file()
    assert asset.name == "sharespace.png"


def test_resource_path_uses_pyinstaller_bundle_root(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resource_path("sharespace.png") == tmp_path / "assets" / "sharespace.png"


def test_resource_path_rejects_directory_traversal():
    with pytest.raises(ValueError, match="simple filenames"):
        resource_path("../private.key")


def test_persistent_log_filter_redacts_credentials_from_messages_and_exceptions():
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(_CredentialRedactionFilter())
    logger = logging.getLogger("sharespace.test.release_logging")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        logger.warning("joining code=%s, Authorization: Bearer %s", "417205", "host-secret")
        logger.warning("hotspot password=%s, operation=start", "private phrase")
        try:
            raise RuntimeError('password="private-passphrase"')
        except RuntimeError:
            logger.exception("Connection failed")
    finally:
        logger.removeHandler(handler)
        handler.close()

    logged = output.getvalue()
    assert "417205" not in logged
    assert "host-secret" not in logged
    assert "private phrase" not in logged
    assert "private-passphrase" not in logged
    assert "[redacted]" in logged


def test_application_smoke_test_exits_cleanly_and_writes_local_database(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PYTHONPATH"] = (
        str(project_root / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    )
    environment["SHARESPACE_SMOKE_DATA_DIR"] = str(tmp_path / "appdata")
    if os.name == "nt":
        environment["LOCALAPPDATA"] = str(tmp_path / "local")
        environment["APPDATA"] = str(tmp_path / "roaming")
    else:
        environment["XDG_DATA_HOME"] = str(tmp_path / "data")
        environment["XDG_STATE_HOME"] = str(tmp_path / "state")

    for launch_number in range(2):
        result = subprocess.run(
            [sys.executable, "-m", "sharespace", "--smoke-test"],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, f"launch {launch_number + 1}: {result.stderr}"
    assert list(tmp_path.rglob("sharespace.sqlite3"))
    assert list(tmp_path.rglob("sharespace.log"))
