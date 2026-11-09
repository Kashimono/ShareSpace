from __future__ import annotations

import os

import pytest

from sharespace.core.file_manager import FileAccessError, MAX_FILE_SIZE_BYTES, SharedDirectoryManager
from sharespace.core.models import FileItemType


def test_directory_listing_is_relative_sorted_and_hides_hidden_entries(tmp_path):
    (tmp_path / "zeta.txt").write_text("z")
    (tmp_path / "áudio.mp3").write_bytes(b"audio")
    (tmp_path / "folder").mkdir()
    (tmp_path / ".secret").write_text("hidden")

    manager = SharedDirectoryManager()
    state = manager.select_root(tmp_path)
    listing = manager.listing()

    assert state.shared is True
    assert state.available is True
    assert state.directory_name == tmp_path.name
    assert [item.filename for item in listing.items] == ["folder", "zeta.txt", "áudio.mp3"]
    assert listing.items[0].item_type is FileItemType.DIRECTORY
    assert listing.items[2].extension == ".mp3"
    assert all(not os.path.isabs(item.relative_path) for item in listing.items)
    with pytest.raises(FileAccessError):
        manager.resolve(".secret", expect_file=True)


@pytest.mark.parametrize("path", ["../outside", "/absolute", r"folder\\file", "folder/../file", "a//b", "C:/file"])
def test_path_validation_rejects_absolute_parent_and_ambiguous_paths(tmp_path, path):
    manager = SharedDirectoryManager()
    manager.select_root(tmp_path)
    with pytest.raises(FileAccessError):
        manager.resolve(path)


def test_selection_rejects_non_directory_and_symlink_root(tmp_path):
    file_path = tmp_path / "file.txt"
    file_path.write_text("x")
    manager = SharedDirectoryManager()
    with pytest.raises(FileAccessError):
        manager.select_root(file_path)

    link = tmp_path / "root-link"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable on this platform.")
    with pytest.raises(FileAccessError):
        manager.select_root(link)


def test_symlink_entries_are_not_listed_or_followed(tmp_path):
    outside = tmp_path.parent / "outside-sharespace.txt"
    outside.write_text("private")
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "escape.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable on this platform.")

    manager = SharedDirectoryManager()
    manager.select_root(root)
    assert manager.listing().items == []
    with pytest.raises(FileAccessError):
        manager.resolve("escape.txt", expect_file=True)


def test_file_size_limit_is_exact_decimal_20_mb():
    assert MAX_FILE_SIZE_BYTES == 20_000_000
