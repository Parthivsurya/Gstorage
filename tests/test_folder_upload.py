"""Tests for how a folder upload lands in the index.

The walk itself is covered in test_local_tree; this is the other half — the
virtual tree the files arrive into, and the check that makes a second run
carry on instead of uploading everything twice.

Both run against a real SQLite index rather than a fake, because the thing
most likely to break is the parent/child wiring between real rows.
"""

from __future__ import annotations

import pytest

from gstore.db.database import Database
from gstore.index import IndexStore
from gstore.local_tree import scan_folder
from gstore.ui.folder_upload_worker import already_there, build_folder_tree


@pytest.fixture
def index(tmp_path):
    db = Database(tmp_path / "idx.db").open().initialize()
    yield IndexStore(db)
    db.close()


def _tree(root, spec):
    for rel, content in spec.items():
        target = root / rel
        if content is None:
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)


# -- building the tree -------------------------------------------------------


def test_the_chosen_folder_becomes_one_folder_at_the_target(index):
    ids = build_folder_tree(index, "Photos", None, [])

    assert set(ids) == {()}
    assert [f.name for f in index.list_folders(None)] == ["Photos"]


def test_nested_directories_are_recreated_under_it(index):
    dirs = [("2024",), ("2024", "jan"), ("2024", "feb")]

    ids = build_folder_tree(index, "Photos", None, dirs)

    root = ids[()]
    assert [f.name for f in index.list_folders(root)] == ["2024"]
    year = ids[("2024",)]
    assert sorted(f.name for f in index.list_folders(year)) == ["feb", "jan"]
    # Each level really is a child of the one above, not a sibling.
    assert index.get_folder(year).parent_id == root
    assert index.get_folder(ids[("2024", "jan")]).parent_id == year


def test_a_deep_chain_keeps_its_order(index):
    dirs = [("a",), ("a", "b"), ("a", "b", "c"), ("a", "b", "c", "d")]

    ids = build_folder_tree(index, "root", None, dirs)

    for rel in dirs:
        parent_rel = rel[:-1]
        assert index.get_folder(ids[rel]).parent_id == ids[parent_rel]


def test_it_nests_under_the_folder_you_were_looking_at(index):
    existing = index.create_folder("Archive", None)

    ids = build_folder_tree(index, "Photos", existing, [("2024",)])

    assert index.get_folder(ids[()]).parent_id == existing


def test_running_it_twice_reuses_the_same_folders(index):
    """A re-run after an interruption must not make Photos, Photos, Photos."""
    dirs = [("2024",), ("2024", "jan")]

    first = build_folder_tree(index, "Photos", None, dirs)
    second = build_folder_tree(index, "Photos", None, dirs)

    assert first == second
    assert len(index.list_folders(None)) == 1
    assert len(index.list_folders(first[()])) == 1


def test_the_real_scan_output_feeds_straight_in(tmp_path, index):
    """scan_folder promises parents-first; this is that promise being used."""
    source = tmp_path / "Photos"
    _tree(source, {"2024/jan/a.txt": b"x", "2024/feb/b.txt": b"y",
                   "2023/c.txt": b"z", "loose.txt": b"w"})

    plan = scan_folder(source)
    ids = build_folder_tree(index, source.name, None, plan.dirs)

    # Every file's directory got an id, which is what the worker indexes into.
    for planned in plan.files:
        assert planned.rel_dir in ids

    root = ids[()]
    assert sorted(f.name for f in index.list_folders(root)) == ["2023", "2024"]


# -- not uploading the same file twice ---------------------------------------


def test_an_empty_folder_holds_nothing_yet(index):
    folder = index.create_folder("Photos", None)

    assert already_there(index, folder, "a.txt") is False


def test_a_file_that_is_already_there_is_recognised(index):
    folder = index.create_folder("Photos", None)
    index.create_file("a.txt", folder, 10, None, None, md5="m")

    assert already_there(index, folder, "a.txt") is True


def test_the_same_name_in_a_different_folder_does_not_count(index):
    """Two folders may each legitimately hold their own a.txt."""
    one = index.create_folder("jan", None)
    two = index.create_folder("feb", None)
    index.create_file("a.txt", one, 10, None, None, md5="m")

    assert already_there(index, one, "a.txt") is True
    assert already_there(index, two, "a.txt") is False


def test_matching_is_exact_not_partial(index):
    folder = index.create_folder("Photos", None)
    index.create_file("report.pdf", folder, 10, None, None, md5="m")

    assert already_there(index, folder, "report") is False
    assert already_there(index, folder, "report.pdf.bak") is False
    assert already_there(index, folder, "report.pdf") is True


def test_a_resumed_run_skips_exactly_what_landed(tmp_path, index):
    """The interrupted-at-file-3-of-5 case, end to end against the index."""
    source = tmp_path / "Photos"
    _tree(source, {f"f{i}.txt": b"x" for i in range(5)})
    plan = scan_folder(source)
    ids = build_folder_tree(index, source.name, None, plan.dirs)
    root = ids[()]

    # Pretend the first three made it before the interruption.
    for planned in plan.files[:3]:
        index.create_file(planned.path.name, root, planned.size_bytes,
                          None, None, md5="m")

    remaining = [p.path.name for p in plan.files
                 if not already_there(index, ids[p.rel_dir], p.path.name)]

    assert remaining == ["f3.txt", "f4.txt"]


# -- the parallel driver -----------------------------------------------------

import threading
from pathlib import Path as _Path

from gstore.local_tree import PlannedFile
from gstore.ui import folder_upload_worker as fuw
from gstore.auth.accounts import Account


def _acct(account_id: int, free_mb: int) -> Account:
    mb = 1024 * 1024
    return Account(
        id=account_id, email=f"a{account_id}@x.com",
        quota_limit_bytes=100 * mb, quota_used_bytes=(100 - free_mb) * mb,
        quota_synced_at="2026-01-01", status="active", created_at="2026-01-01",
    )


class _Accounts:
    def get_credentials(self, account_id):
        return f"creds{account_id}"


class _Folders:
    def folder_id(self, account_id):
        return f"gstore-folder-{account_id}"


class _Result:
    def __init__(self, drive_id):
        self.drive_file_id = drive_id
        self.size_bytes = 10
        self.md5 = "m"


def _planned(n: int) -> list[PlannedFile]:
    return [
        PlannedFile(path=_Path(f"/tmp/f{i}.txt"), rel_dir=(), size_bytes=10)
        for i in range(n)
    ]


@pytest.fixture
def worker():
    w = fuw.FolderUploadWorker("/tmp/folder", None)
    w._done = 0
    return w


@pytest.fixture
def linked(index):
    """Real account rows, because files.home_account_id is a foreign key.

    The constraint is right and the fixture was wrong: an index row claiming
    an account that does not exist is exactly what it is there to stop.
    """
    for account_id in (1, 2):
        index.db.conn.execute(
            "INSERT INTO accounts(id, email) VALUES(?, ?)",
            (account_id, f"a{account_id}@x.com"),
        )
    index.db.conn.commit()
    return index


def test_every_small_file_is_uploaded_and_recorded(linked, worker, monkeypatch):
    index = linked
    files = _planned(12)
    calls: list[str] = []
    lock = threading.Lock()

    def fake_upload(creds, path, parent_id=None, **kw):
        with lock:
            calls.append(str(path))
        return _Result(f"drive-{_Path(path).name}")

    monkeypatch.setattr(fuw, "upload_file", fake_upload)
    root = index.create_folder("folder", None)

    uploaded, failed = worker._upload_in_parallel(
        files, {(): root}, _Accounts(), index, _Folders(),
        [_acct(1, 50), _acct(2, 50)], len(files),
    )

    assert (uploaded, failed) == (12, 0)
    assert len(calls) == 12
    assert len(index.list_files(root)) == 12, "each landed in the index once"


def test_the_upload_really_runs_concurrently(linked, worker, monkeypatch):
    index = linked
    """The whole point: 12 files must not be 12 sequential round trips."""
    import time

    peak = 0
    live = 0
    lock = threading.Lock()

    def fake_upload(creds, path, parent_id=None, **kw):
        nonlocal peak, live
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return _Result(f"drive-{_Path(path).name}")

    monkeypatch.setattr(fuw, "upload_file", fake_upload)
    root = index.create_folder("folder", None)

    worker._upload_in_parallel(
        _planned(12), {(): root}, _Accounts(), index, _Folders(),
        [_acct(1, 50)], 12,
    )

    assert peak > 1, "files were sent one at a time"


def test_one_failure_is_counted_and_the_rest_still_land(linked, worker, monkeypatch):
    index = linked
    def fake_upload(creds, path, parent_id=None, **kw):
        if _Path(path).name == "f3.txt":
            raise RuntimeError("Drive said no")
        return _Result(f"drive-{_Path(path).name}")

    monkeypatch.setattr(fuw, "upload_file", fake_upload)
    root = index.create_folder("folder", None)

    uploaded, failed = worker._upload_in_parallel(
        _planned(8), {(): root}, _Accounts(), index, _Folders(),
        [_acct(1, 50)], 8,
    )

    assert (uploaded, failed) == (7, 1)
    assert "f3.txt" not in {e.name for e in index.list_files(root)}


def test_a_file_too_big_for_every_account_is_reported_not_crashed(
    index, worker, monkeypatch
):
    monkeypatch.setattr(fuw, "upload_file", lambda *a, **k: _Result("x"))
    root = index.create_folder("folder", None)
    huge = [PlannedFile(path=_Path("/tmp/huge.bin"), rel_dir=(),
                        size_bytes=999 * 1024 * 1024)]

    uploaded, failed = worker._upload_in_parallel(
        huge, {(): root}, _Accounts(), index, _Folders(), [_acct(1, 5)], 1,
    )

    assert (uploaded, failed) == (0, 1)


def test_nothing_to_send_is_not_an_error(index, worker, monkeypatch):
    monkeypatch.setattr(fuw, "upload_file", lambda *a, **k: _Result("x"))

    assert worker._upload_in_parallel(
        [], {}, _Accounts(), index, _Folders(), [_acct(1, 50)], 0
    ) == (0, 0)


def test_each_file_goes_to_its_planned_accounts_folder(linked, worker, monkeypatch):
    index = linked
    seen: dict[str, str] = {}
    lock = threading.Lock()

    def fake_upload(creds, path, parent_id=None, **kw):
        with lock:
            seen[_Path(path).name] = parent_id
        return _Result("x")

    monkeypatch.setattr(fuw, "upload_file", fake_upload)
    root = index.create_folder("folder", None)

    worker._upload_in_parallel(
        _planned(6), {(): root}, _Accounts(), index, _Folders(),
        [_acct(1, 50), _acct(2, 50)], 6,
    )

    assert all(v.startswith("gstore-folder-") for v in seen.values()), seen


# -- progress across the batch, not per file ---------------------------------


def _progress_of(worker) -> list[float]:
    seen: list[float] = []
    worker.progress.connect(seen.append)
    return seen


def test_progress_counts_bytes_across_the_whole_batch(linked, worker, monkeypatch):
    """The 0.0% bug: the bar showed one file, so it never left zero."""
    index = linked
    monkeypatch.setattr(fuw, "upload_file", lambda *a, **k: _Result("x"))
    root = index.create_folder("folder", None)
    files = [
        PlannedFile(path=_Path(f"/tmp/f{i}.txt"), rel_dir=(), size_bytes=100)
        for i in range(4)
    ]
    worker._batch_bytes = 400
    worker._bytes_done = 0
    seen = _progress_of(worker)

    worker._upload_in_parallel(
        files, {(): root}, _Accounts(), index, _Folders(), [_acct(1, 50)], 4
    )

    assert seen, "no progress was reported at all"
    assert max(seen) == pytest.approx(1.0), "the batch never reached 100%"
    assert sorted(seen) == seen, "progress went backwards"


def test_progress_is_weighted_by_size_not_file_count(linked, worker, monkeypatch):
    """20 KB and 2 MB are both 'one file'; only bytes predict time left."""
    index = linked
    done_order: list[str] = []

    def fake_upload(creds, path, parent_id=None, **kw):
        done_order.append(_Path(path).name)
        return _Result("x")

    monkeypatch.setattr(fuw, "upload_file", fake_upload)
    root = index.create_folder("folder", None)
    files = [
        PlannedFile(path=_Path("/tmp/small.txt"), rel_dir=(), size_bytes=10),
        PlannedFile(path=_Path("/tmp/big.txt"), rel_dir=(), size_bytes=990),
    ]
    worker._batch_bytes = 1000
    worker._bytes_done = 0
    seen = _progress_of(worker)

    worker._upload_in_parallel(
        files, {(): root}, _Accounts(), index, _Folders(), [_acct(1, 50)], 2
    )

    assert max(seen) == pytest.approx(1.0)
    # Finishing only the small file must not read as half done.
    assert any(v == pytest.approx(0.01) or v == pytest.approx(0.99) for v in seen)


def test_a_failed_file_still_advances_the_bar(linked, worker, monkeypatch):
    """Otherwise the reading sticks for every file that cannot go up."""
    index = linked

    def fake_upload(creds, path, parent_id=None, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(fuw, "upload_file", fake_upload)
    root = index.create_folder("folder", None)
    files = [
        PlannedFile(path=_Path(f"/tmp/f{i}.txt"), rel_dir=(), size_bytes=50)
        for i in range(2)
    ]
    worker._batch_bytes = 100
    worker._bytes_done = 0
    seen = _progress_of(worker)

    uploaded, failed = worker._upload_in_parallel(
        files, {(): root}, _Accounts(), index, _Folders(), [_acct(1, 50)], 2
    )

    assert (uploaded, failed) == (0, 2)
    assert max(seen) == pytest.approx(1.0)


def test_a_large_files_own_progress_is_scaled_into_the_batch(worker):
    """A 4 MB file hitting 100% is not the folder hitting 100%."""
    worker._batch_bytes = 1000
    worker._bytes_done = 500   # half the batch already landed
    worker._current_size = 500  # this file is the other half
    seen = _progress_of(worker)

    worker._emit_progress(0.0)
    worker._emit_progress(0.5)
    worker._emit_progress(1.0)

    assert seen == [pytest.approx(0.5), pytest.approx(0.75), pytest.approx(1.0)]


def test_progress_never_exceeds_one(worker):
    worker._batch_bytes = 100
    worker._bytes_done = 100
    worker._current_size = 100
    seen = _progress_of(worker)

    worker._emit_progress(1.0)

    assert seen == [pytest.approx(1.0)]


def test_no_progress_is_reported_when_the_batch_is_empty(worker):
    worker._batch_bytes = 0
    seen = _progress_of(worker)

    worker._emit_progress(0.5)

    assert seen == [], "dividing by an empty batch must not be attempted"


# -- cancelling must not strand files on Drive -------------------------------


def test_cancelling_records_the_files_already_in_the_air(
    linked, worker, monkeypatch
):
    """A file that reached Drive without an index row is an orphan.

    The next run would upload a second copy and only Repair / re-sync would
    ever find the first, so stopping must drain what is already in flight.
    """
    import time

    index = linked
    sent: list[str] = []
    lock = threading.Lock()

    def fake_upload(creds, path, parent_id=None, **kw):
        time.sleep(0.05)          # still in the air when the stop lands
        with lock:
            sent.append(_Path(path).name)
        return _Result(f"drive-{_Path(path).name}")

    monkeypatch.setattr(fuw, "upload_file", fake_upload)

    # Stop as soon as the first result is handled.
    calls = {"n": 0}

    def interrupted():
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(worker, "isInterruptionRequested", interrupted)
    root = index.create_folder("folder", None)
    worker._batch_bytes = 16 * 10
    worker._bytes_done = 0

    uploaded, failed = worker._upload_in_parallel(
        _planned(16), {(): root}, _Accounts(), index, _Folders(),
        [_acct(1, 50)], 16,
    )

    recorded = {e.name for e in index.list_files(root)}
    assert worker._stopped is True, "the run should have stopped"
    assert set(sent) <= recorded, (
        f"uploaded to Drive but missing from the index: {set(sent) - recorded}"
    )
    assert uploaded == len(recorded)


def test_nothing_is_recorded_twice_when_stopping(linked, worker, monkeypatch):
    """The drain must skip futures the normal loop already handled."""
    index = linked
    monkeypatch.setattr(fuw, "upload_file", lambda *a, **k: _Result("x"))

    calls = {"n": 0}

    def interrupted():
        calls["n"] += 1
        return calls["n"] > 3

    monkeypatch.setattr(worker, "isInterruptionRequested", interrupted)
    root = index.create_folder("folder", None)
    worker._batch_bytes = 80
    worker._bytes_done = 0

    uploaded, _ = worker._upload_in_parallel(
        _planned(8), {(): root}, _Accounts(), index, _Folders(),
        [_acct(1, 50)], 8,
    )

    names = [e.name for e in index.list_files(root)]
    assert len(names) == len(set(names)), f"duplicate index rows: {names}"
    assert uploaded == len(names)


def test_a_clean_run_is_unaffected_by_the_drain(linked, worker, monkeypatch):
    """No interruption: every file goes through the normal path exactly once."""
    index = linked
    monkeypatch.setattr(fuw, "upload_file", lambda *a, **k: _Result("x"))
    root = index.create_folder("folder", None)
    worker._batch_bytes = 100
    worker._bytes_done = 0

    uploaded, failed = worker._upload_in_parallel(
        _planned(10), {(): root}, _Accounts(), index, _Folders(),
        [_acct(1, 50)], 10,
    )

    assert (uploaded, failed) == (10, 0)
    assert worker._stopped is False
    assert len(index.list_files(root)) == 10


# -- run() end to end --------------------------------------------------------
#
# Every test above calls _upload_in_parallel directly, which is exactly why a
# missing `total_bytes` signal on run()'s path shipped unnoticed: the method
# was fine and the method around it was not. These drive run() itself.


class _Quota:
    limit_bytes = 100 * 1024 * 1024
    used_bytes = 0


def _run_folder_upload(tmp_path, monkeypatch, spec):
    """Drive FolderUploadWorker.run() against a temp index and a fake Drive."""
    from gstore.auth.accounts import AccountStore
    from gstore.db.database import Database as RealDatabase

    source = tmp_path / "Pics"
    for rel, content in spec.items():
        target = source / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    db_path = tmp_path / "idx.db"
    db = RealDatabase(db_path).open().initialize()
    db.conn.execute(
        "INSERT OR IGNORE INTO accounts(id, email) VALUES(1, 'a@x.com')"
    )
    db.conn.commit()
    db.close()

    monkeypatch.setattr(fuw, "Database", lambda: RealDatabase(db_path))
    monkeypatch.setattr(fuw, "fetch_storage_quota", lambda creds: _Quota())
    monkeypatch.setattr(fuw, "upload_file", lambda *a, **k: _Result("drive-x"))
    monkeypatch.setattr(
        AccountStore, "get_credentials", lambda self, account_id: "creds"
    )
    monkeypatch.setattr(
        fuw, "FolderResolver", lambda accounts: _Folders()
    )

    worker = fuw.FolderUploadWorker(str(source), None)
    events: dict[str, list] = {
        "summary": [], "failed": [], "totals": [], "progress": []
    }
    worker.finished_summary.connect(
        lambda u, s, f: events["summary"].append((u, s, f))
    )
    worker.failed.connect(events["failed"].append)
    worker.total_bytes.connect(events["totals"].append)
    worker.progress.connect(events["progress"].append)
    worker.run()                       # directly, not start(): no thread
    return worker, events, db_path


def test_run_completes_without_an_attribute_error(tmp_path, monkeypatch):
    """The regression: run() emitted total_bytes, a signal it did not have."""
    _, events, _ = _run_folder_upload(
        tmp_path, monkeypatch, {"a.png": b"x" * 10, "b.png": b"y" * 20}
    )

    assert events["failed"] == [], f"run() reported: {events['failed']}"
    assert events["summary"] == [(2, 0, 0)]


def test_run_announces_the_batch_size_before_uploading(tmp_path, monkeypatch):
    _, events, _ = _run_folder_upload(
        tmp_path, monkeypatch, {"a.png": b"x" * 10, "sub/b.png": b"y" * 20}
    )

    assert events["totals"] == [30], "the UI needs the batch size for its ETA"
    assert max(events["progress"]) == pytest.approx(1.0)


def test_run_recreates_the_folder_tree_in_the_index(tmp_path, monkeypatch):
    from gstore.db.database import Database as RealDatabase

    _, events, db_path = _run_folder_upload(
        tmp_path, monkeypatch,
        {"top.png": b"x", "2024/jan/deep.png": b"y", "2024/feb/other.png": b"z"},
    )

    assert events["failed"] == []
    db = RealDatabase(db_path).open()
    try:
        names = {
            r["name"] for r in db.conn.execute("SELECT name FROM folders")
        }
        assert {"Pics", "2024", "jan", "feb"} <= names
        count = db.conn.execute("SELECT count(*) c FROM files").fetchone()["c"]
        assert count == 3
    finally:
        db.close()


def test_run_a_second_time_uploads_nothing(tmp_path, monkeypatch):
    """The resume path through run(): same folder, same index, no new work."""
    spec = {"a.png": b"x" * 10, "b.png": b"y" * 20}
    _, first, _ = _run_folder_upload(tmp_path, monkeypatch, spec)
    _, second, _ = _run_folder_upload(tmp_path, monkeypatch, spec)

    assert first["summary"] == [(2, 0, 0)], "first run uploads both"
    assert second["summary"] == [(0, 2, 0)], "second run skips both"
    assert second["failed"] == []


def test_an_empty_folder_is_reported_not_crashed(tmp_path, monkeypatch):
    source = tmp_path / "Pics"
    source.mkdir()
    _, events, _ = _run_folder_upload(tmp_path, monkeypatch, {})

    assert events["summary"] == []
    assert len(events["failed"]) == 1
    assert "nothing in it" in events["failed"][0]
