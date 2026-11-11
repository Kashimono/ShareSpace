"""Resolve bundled resources independently of the process working directory."""

from __future__ import annotations

import sys
from pathlib import Path


def resource_path(name: str) -> Path:
    """Return an asset path for both source-tree and PyInstaller execution."""
    if Path(name).name != name:
        raise ValueError("Resource names must be simple filenames.")
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root is not None:
        return Path(bundle_root) / "assets" / name
    repository_root = Path(__file__).resolve().parents[3]
    return repository_root / "assets" / name
