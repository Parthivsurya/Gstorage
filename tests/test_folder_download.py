"""Tests for rebuilding a folder's structure on disk when downloading it.

Two things here are not cosmetic:

* **Only the chosen folder's structure is recreated.** Downloading "2024"
  must not also recreate every folder above it inside the destination.
* **A name from the index may not escape the destination.** Index names are
  whatever the user typed, and a "/" or ".." in one would otherwise write
  outside the folder they picked.
"""

from __future__ import annotations

import pytest

from gstore.db.database import Database
from gstore.index import IndexStore
from gstore.ui.folder_download_worker import (
    FolderDownloadWorker,
    relative_dirs,
    safe_name,
)


@pytest.fixture
def index(tmp_path):
    db = Database(tmp_path / "idx.db").open().initialize()
    yield IndexStore(db)
    db.close()


# -- where each file goes ----------------------------------------------------


def test_a_file_directly_in_the_chosen_folder_has_no_subpath(index):
    photos = index.create_folder("Photos", None)

    assert relative_dirs(index, photos, photos) == ()


def test_a_nested_file_keeps_only_the_part_below_the_chosen_folder(index):
    photos = index.create_folder("Photos", None)
    year = index.create_folder("2024", photos)
    month = index.create_folder("jan", year)

    assert relative_dirs(index, photos, month) == ("2024", "jan")


def test_folders_above_the_chosen_one_are_dropped(index):
    """Downloading '2024' must not recreate Archive/Photos around it."""
    archive = index.create_folder("Archive", None)
    photos = index.create_folder("Photos", archive)
    year = index.create_folder("2024", photos)
    month = index.create_folder("jan", year)

    assert relative_dirs(index, year, month) == ("jan",)
    assert relative_dirs(index, year, year) == ()


def test_a_parent_outside_the_chosen_subtree_yields_nothing(index):
    """Defensive: never return a path that would climb out of the target."""
    photos = index.create_folder("Photos", None)
    other = index.create_folder("Documents", None)

    assert relative_dirs(index, photos, other) == ()


def test_a_file_at_the_virtual_root_has_no_subpath(index):
    photos = index.create_folder("Photos", None)

    assert relative_dirs(index, photos, None) == ()


# -- names from the index may not escape -------------------------------------


def test_a_slash_in_a_name_cannot_make_a_subdirectory():
    assert "/" not in safe_name("quarterly/report.pdf")
    assert safe_name("quarterly/report.pdf") == "quarterly_report.pdf"


def test_a_backslash_is_neutralised_too():
    assert safe_name(r"win\path.txt") == "win_path.txt"


def test_dot_dot_cannot_climb_out_of_the_destination():
    assert safe_name("..") == "_"
    assert safe_name(".") == "_"


def test_an_empty_or_blank_name_still_yields_something_writable():
    assert safe_name("") == "_"
    assert safe_name("   ") == "_"


def test_an_ordinary_name_is_left_alone():
    assert safe_name("Report 2024 (final).pdf") == "Report 2024 (final).pdf"
    assert safe_name("imp files.rar") == "imp files.rar"


def test_a_leading_dot_name_is_kept_as_is():
    """Hidden on disk is fine; it is only traversal that matters here."""
    assert safe_name(".config") == ".config"


# -- skipping what is already on disk ----------------------------------------


def test_a_file_at_the_right_size_is_skipped(tmp_path):
    target = tmp_path / "a.bin"
    target.write_bytes(b"x" * 100)

    assert FolderDownloadWorker._already_downloaded(target, 100) is True


def test_a_truncated_file_is_fetched_again(tmp_path):
    """A half-written file from an interrupted run must not be mistaken for done."""
    target = tmp_path / "a.bin"
    target.write_bytes(b"x" * 40)

    assert FolderDownloadWorker._already_downloaded(target, 100) is False


def test_a_missing_file_is_fetched(tmp_path):
    assert FolderDownloadWorker._already_downloaded(tmp_path / "nope.bin", 10) is False


def test_a_directory_where_the_file_should_be_is_not_mistaken_for_it(tmp_path):
    target = tmp_path / "a.bin"
    target.mkdir()

    assert FolderDownloadWorker._already_downloaded(target, 0) is False


def test_a_zero_byte_file_counts_as_present(tmp_path):
    target = tmp_path / "empty.bin"
    target.write_bytes(b"")

    assert FolderDownloadWorker._already_downloaded(target, 0) is True


# -- the whole shape, against a real index -----------------------------------


def test_every_descendant_resolves_to_a_path_inside_the_destination(index, tmp_path):
    photos = index.create_folder("Photos", None)
    year = index.create_folder("2024", photos)
    month = index.create_folder("jan", year)
    index.create_file("loose.txt", photos, 1, None, None, md5="m")
    index.create_file("a.txt", year, 1, None, None, md5="m")
    index.create_file("../escape.txt", month, 1, None, None, md5="m")

    root = (tmp_path / "dest" / safe_name("Photos")).resolve()
    for entry in index.descendant_files(photos):
        rel = relative_dirs(index, photos, entry.parent_id)
        path = root.joinpath(*(safe_name(p) for p in rel)) / safe_name(entry.name)
        assert root in path.resolve().parents or path.resolve().parent == root, (
            f"{entry.name} resolved outside the destination: {path}"
        )
