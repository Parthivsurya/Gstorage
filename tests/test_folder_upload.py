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
