"""Tests for the live Drive listing mapper (gstore.drive.browse).

The scan view is only as trustworthy as this record→DriveFile mapping: it must
classify folders and Google-native docs correctly (they carry no byte size and,
for docs, can't be streamed via get_media), parse sizes, and correctly report
whether credentials can actually see the whole Drive.
"""

from __future__ import annotations

from types import SimpleNamespace

from gstore.auth.oauth import (
    DRIVE_FILE_SCOPE,
    DRIVE_FULL_SCOPE,
    DRIVE_READONLY_SCOPE,
)
from gstore.drive.browse import _to_drive_file, can_modify, has_full_read


def test_regular_binary_file_is_downloadable():
    f = _to_drive_file(
        {
            "id": "abc",
            "name": "clip.mp4",
            "size": "1048576",
            "mimeType": "video/mp4",
            "md5Checksum": "deadbeef",
        }
    )
    assert f.size == 1048576
    assert not f.is_folder and not f.is_google_doc
    assert f.is_downloadable


def test_folder_is_not_downloadable_and_has_no_size():
    f = _to_drive_file(
        {"id": "f1", "name": "Photos", "mimeType": "application/vnd.google-apps.folder"}
    )
    assert f.is_folder
    assert f.size is None
    assert not f.is_downloadable


def test_google_doc_is_export_only():
    f = _to_drive_file(
        {"id": "d1", "name": "Notes", "mimeType": "application/vnd.google-apps.document"}
    )
    assert f.is_google_doc
    assert not f.is_folder
    assert not f.is_downloadable  # export-only, no direct bytes


def test_has_full_read_reflects_granted_scopes():
    readonly = SimpleNamespace(scopes=[DRIVE_FILE_SCOPE, DRIVE_READONLY_SCOPE])
    full = SimpleNamespace(scopes=[DRIVE_FILE_SCOPE, DRIVE_FULL_SCOPE])
    narrow = SimpleNamespace(scopes=[DRIVE_FILE_SCOPE])
    none = SimpleNamespace(scopes=None)
    assert has_full_read(readonly)
    assert has_full_read(full)  # full drive also sees everything
    assert not has_full_read(narrow)
    assert not has_full_read(none)


def test_can_modify_requires_full_drive_scope():
    full = SimpleNamespace(scopes=[DRIVE_FILE_SCOPE, DRIVE_FULL_SCOPE])
    readonly = SimpleNamespace(scopes=[DRIVE_FILE_SCOPE, DRIVE_READONLY_SCOPE])
    narrow = SimpleNamespace(scopes=[DRIVE_FILE_SCOPE])
    # Only the full drive scope may trash arbitrary files; readonly/drive.file
    # can see or manage only their own, so must not enable delete.
    assert can_modify(full)
    assert not can_modify(readonly)
    assert not can_modify(narrow)
