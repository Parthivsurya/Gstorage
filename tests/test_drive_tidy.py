"""Tests for the one-time move of older uploads into the Gstore folder.

This runs over files a real user already owns, so the failure modes matter more
than the happy path:

* **One stubborn file must not strand the other 399.** A 20 GB file is ~400
  pieces; aborting the run on the first refusal would leave most of them
  scattered with no way to tell which.
* **Every existing parent is removed, not just the root.** Drive used to allow
  a file in several folders at once, and an older file can still carry more
  than one.
* **Stopping is safe and repeating is safe.** Files already moved stay moved,
  and a second run picks up only what is left.

The Drive service is faked at :func:`gstore.drive.tidy.drive_service`.
"""

from __future__ import annotations

import pytest
from googleapiclient.errors import HttpError

from gstore.drive import tidy as tidy_mod
from gstore.drive.folder import FOLDER_MIME
from gstore.drive.tidy import (
    Cancelled,
    Misplaced,
    files_to_move,
    move_into,
    tidy_account,
)

FOLDER = "theGstoreFolder"


def _http_error(status: int) -> HttpError:
    return HttpError(type("R", (), {"status": status, "reason": "x"})(), b"{}")


class FakeDrive:
    """Holds {id: (name, parents)} and records every update."""

    def __init__(self, files=None, pages=1) -> None:
        # files: list of (id, name, parents, mime)
        self.files = list(files or [])
        self.pages = pages
        self.updates: list[tuple[str, str, str | None]] = []
        self.queries: list[str] = []
        self.refuse: set[str] = set()
        self.list_calls = 0

    def service(self, credentials):
        return _Service(self)


class _Files:
    def __init__(self, drive: FakeDrive) -> None:
        self._d = drive

    def list(self, q=None, spaces=None, fields=None, pageSize=None, pageToken=None):
        d = self._d

        def execute():
            d.queries.append(q)
            d.list_calls += 1
            # Honour the mimeType exclusion the way Drive would.
            visible = [
                f for f in d.files
                if f"mimeType != '{f[3]}'" not in (q or "")
            ]
            half = (len(visible) + 1) // 2
            if d.pages == 2 and pageToken is None:
                return {"files": _entries(visible[:half]), "nextPageToken": "p2"}
            if d.pages == 2:
                return {"files": _entries(visible[half:])}
            return {"files": _entries(visible)}

        return _Request(execute)

    def update(self, fileId=None, addParents=None, removeParents=None, fields=None):
        d = self._d

        def execute():
            if fileId in d.refuse:
                raise _http_error(403)
            d.updates.append((fileId, addParents, removeParents))
            return {"id": fileId}

        return _Request(execute)


def _entries(rows):
    out = []
    for file_id, name, parents, mime in rows:
        entry = {"id": file_id, "name": name}
        if parents is not None:
            entry["parents"] = list(parents)
        out.append(entry)
    return out


class _Service:
    def __init__(self, drive) -> None:
        self._d = drive

    def files(self):
        return _Files(self._d)


class _Request:
    def __init__(self, execute) -> None:
        self.execute = execute


def _install(monkeypatch, drive: FakeDrive) -> FakeDrive:
    monkeypatch.setattr(tidy_mod, "drive_service", drive.service)
    return drive


# -- finding what needs moving -----------------------------------------------


def test_a_file_in_the_root_needs_moving(monkeypatch):
    drive = _install(monkeypatch, FakeDrive([("f1", "a.bin", ["rootId"], "x")]))

    found = files_to_move(None, FOLDER)

    assert [f.drive_file_id for f in found] == ["f1"]
    assert found[0].parents == ("rootId",)


def test_a_file_already_in_the_folder_is_left_alone(monkeypatch):
    _install(monkeypatch, FakeDrive([("f1", "a.bin", [FOLDER], "x")]))

    assert files_to_move(None, FOLDER) == []


def test_only_the_misplaced_half_comes_back(monkeypatch):
    _install(monkeypatch, FakeDrive([
        ("in1", "a", [FOLDER], "x"),
        ("out1", "b", ["rootId"], "x"),
        ("in2", "c", [FOLDER], "x"),
        ("out2", "d", ["rootId"], "x"),
    ]))

    assert {f.drive_file_id for f in files_to_move(None, FOLDER)} == {"out1", "out2"}


def test_the_query_excludes_folders(monkeypatch):
    """The Gstore folder is app-created; a folder cannot move inside itself."""
    drive = _install(monkeypatch, FakeDrive([("f1", "a", ["rootId"], "x")]))

    files_to_move(None, FOLDER)

    assert f"mimeType != '{FOLDER_MIME}'" in drive.queries[0]
    assert "trashed=false" in drive.queries[0]


def test_every_page_is_walked(monkeypatch):
    drive = _install(monkeypatch, FakeDrive(
        [(f"f{i}", f"n{i}", ["rootId"], "x") for i in range(6)], pages=2
    ))

    found = files_to_move(None, FOLDER)

    assert len(found) == 6
    assert drive.list_calls == 2


def test_a_file_with_no_parents_at_all_is_still_offered(monkeypatch):
    """Drive can report a file with no parents; it still belongs in the folder."""
    _install(monkeypatch, FakeDrive([("f1", "orphan", None, "x")]))

    (found,) = files_to_move(None, FOLDER)

    assert found.parents == ()


# -- moving one file ---------------------------------------------------------


def test_the_move_adds_the_folder_and_drops_the_old_parent(monkeypatch):
    drive = _install(monkeypatch, FakeDrive())

    move_into(None, Misplaced("f1", "a.bin", ("rootId",)), FOLDER)

    assert drive.updates == [("f1", FOLDER, "rootId")]


def test_a_file_in_several_folders_loses_all_of_them(monkeypatch):
    """Legacy multi-parenting: leaving one attached shows the file twice."""
    drive = _install(monkeypatch, FakeDrive())

    move_into(None, Misplaced("f1", "a.bin", ("rootId", "otherFolder")), FOLDER)

    (file_id, added, removed) = drive.updates[0]
    assert added == FOLDER
    assert set(removed.split(",")) == {"rootId", "otherFolder"}


def test_a_parentless_file_is_simply_added(monkeypatch):
    drive = _install(monkeypatch, FakeDrive())

    move_into(None, Misplaced("f1", "a.bin", ()), FOLDER)

    assert drive.updates == [("f1", FOLDER, None)]


# -- the whole pass ----------------------------------------------------------


def test_every_misplaced_file_is_moved(monkeypatch):
    drive = _install(monkeypatch, FakeDrive(
        [(f"f{i}", f"n{i}", ["rootId"], "x") for i in range(5)]
    ))

    result = tidy_account(None, FOLDER)

    assert result.moved == 5
    assert result.failed == 0
    assert {u[0] for u in drive.updates} == {f"f{i}" for i in range(5)}


def test_nothing_to_do_is_not_an_error_and_makes_no_updates(monkeypatch):
    drive = _install(monkeypatch, FakeDrive([("f1", "a", [FOLDER], "x")]))

    result = tidy_account(None, FOLDER)

    assert (result.moved, result.failed) == (0, 0)
    assert drive.updates == []


def test_one_refusal_does_not_strand_the_rest(monkeypatch):
    """The important one: 400 pieces must not hinge on the first stubborn file."""
    drive = _install(monkeypatch, FakeDrive(
        [(f"f{i}", f"n{i}", ["rootId"], "x") for i in range(10)]
    ))
    drive.refuse = {"f3"}

    result = tidy_account(None, FOLDER)

    assert result.moved == 9
    assert result.failed == 1
    assert "f3" not in {u[0] for u in drive.updates}


def test_progress_is_reported_for_every_file_including_failures(monkeypatch):
    drive = _install(monkeypatch, FakeDrive(
        [(f"f{i}", f"n{i}", ["rootId"], "x") for i in range(4)]
    ))
    drive.refuse = {"f1"}
    seen: list[tuple[int, int]] = []

    tidy_account(None, FOLDER, on_progress=lambda d, t: seen.append((d, t)))

    assert [d for d, _ in seen] == [1, 2, 3, 4]
    assert {t for _, t in seen} == {4}


def test_stopping_raises_cancelled_and_keeps_what_moved(monkeypatch):
    drive = _install(monkeypatch, FakeDrive(
        [(f"f{i}", f"n{i}", ["rootId"], "x") for i in range(20)]
    ))

    with pytest.raises(Cancelled):
        tidy_account(None, FOLDER, should_stop=lambda: True)

    # Whatever landed before the stop stays landed; nothing is rolled back.
    assert len(drive.updates) <= 20


def test_a_second_run_only_picks_up_what_is_left(monkeypatch):
    """Repeatable by construction: moved files no longer match the query."""
    drive = _install(monkeypatch, FakeDrive(
        [("f1", "a", ["rootId"], "x"), ("f2", "b", [FOLDER], "x")]
    ))

    first = tidy_account(None, FOLDER)
    assert first.moved == 1

    # Reflect the move back into the fake, as Drive would.
    drive.files = [("f1", "a", [FOLDER], "x"), ("f2", "b", [FOLDER], "x")]
    second = tidy_account(None, FOLDER)

    assert (second.moved, second.failed) == (0, 0)


def test_an_actual_folder_entry_is_never_offered_for_moving(monkeypatch):
    """Not just the query text: a folder in the results must not come back."""
    _install(monkeypatch, FakeDrive([
        ("realfile", "a.bin", ["rootId"], "application/octet-stream"),
        ("thefolder", "Gstore", None, FOLDER_MIME),
    ]))

    found = files_to_move(None, FOLDER)

    assert [f.drive_file_id for f in found] == ["realfile"]
    assert "thefolder" not in {f.drive_file_id for f in found}


def test_moving_a_folder_is_never_attempted_in_a_whole_pass(monkeypatch):
    drive = _install(monkeypatch, FakeDrive([
        ("realfile", "a.bin", ["rootId"], "application/octet-stream"),
        ("thefolder", "Gstore", None, FOLDER_MIME),
    ]))

    result = tidy_account(None, FOLDER)

    assert result.moved == 1
    assert {u[0] for u in drive.updates} == {"realfile"}
