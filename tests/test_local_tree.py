"""Tests for deciding what a folder upload actually uploads.

These cover the choices that would otherwise surprise someone who picked a
folder expecting it to arrive as it looked on disk:

* a symlink pointing at a parent must not make the walk run forever
* a stray `.git` must not turn "upload Photos" into thousands of files
* an empty subfolder must still come back
* one unreadable file must not take the other 499 with it
"""

from __future__ import annotations

import os

import pytest

from gstore.local_tree import (
    SKIPPED_DEPENDENCY,
    SKIPPED_HIDDEN,
    SKIPPED_SYMLINK,
    SKIPPED_UNREADABLE,
    scan_folder,
)


def _tree(root, spec: dict) -> None:
    """Build {relative path: contents or None-for-dir} under `root`."""
    for rel, content in spec.items():
        target = root / rel
        if content is None:
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)


def _names(plan) -> set[str]:
    return {f.path.name for f in plan.files}


# -- the straightforward shape -----------------------------------------------


def test_files_at_the_top_have_no_relative_directory(tmp_path):
    _tree(tmp_path, {"a.txt": b"aa", "b.txt": b"bbb"})

    plan = scan_folder(tmp_path)

    assert _names(plan) == {"a.txt", "b.txt"}
    assert all(f.rel_dir == () for f in plan.files)
    assert plan.total_bytes == 5
    assert plan.file_count == 2


def test_nested_files_carry_their_directory_path(tmp_path):
    _tree(tmp_path, {"2024/jan/a.txt": b"x", "2024/b.txt": b"yy"})

    plan = scan_folder(tmp_path)

    by_name = {f.path.name: f.rel_dir for f in plan.files}
    assert by_name["a.txt"] == ("2024", "jan")
    assert by_name["b.txt"] == ("2024",)


def test_directories_come_back_parents_first(tmp_path):
    """A caller creates them in order; a child before its parent would fail."""
    _tree(tmp_path, {"a/b/c/deep.txt": b"x"})

    plan = scan_folder(tmp_path)

    assert plan.dirs == [("a",), ("a", "b"), ("a", "b", "c")]
    for i, rel in enumerate(plan.dirs):
        if len(rel) > 1:
            assert rel[:-1] in plan.dirs[:i], f"{rel} came before its parent"


def test_an_empty_subfolder_survives_the_trip(tmp_path):
    _tree(tmp_path, {"photos": None, "photos/empty": None, "photos/a.txt": b"x"})

    plan = scan_folder(tmp_path)

    assert ("photos", "empty") in plan.dirs
    assert plan.file_count == 1


def test_an_entirely_empty_folder_yields_nothing_but_does_not_raise(tmp_path):
    plan = scan_folder(tmp_path)

    assert plan.files == []
    assert plan.dirs == []
    assert plan.total_bytes == 0


def test_a_zero_byte_file_is_still_uploaded(tmp_path):
    _tree(tmp_path, {"empty.bin": b""})

    plan = scan_folder(tmp_path)

    assert _names(plan) == {"empty.bin"}
    assert plan.files[0].size_bytes == 0


# -- the things that get skipped ---------------------------------------------


def test_dot_files_are_skipped_and_reported(tmp_path):
    _tree(tmp_path, {"a.txt": b"x", ".DS_Store": b"junk"})

    plan = scan_folder(tmp_path)

    assert _names(plan) == {"a.txt"}
    assert any(p.name == ".DS_Store" and why == SKIPPED_HIDDEN
               for p, why in plan.skipped)


def test_a_dot_directory_is_not_descended_into(tmp_path):
    """Otherwise picking 'Photos' quietly uploads the whole of .git."""
    _tree(tmp_path, {"a.txt": b"x", ".git/objects/deadbeef": b"blob",
                     ".git/config": b"cfg"})

    plan = scan_folder(tmp_path)

    assert _names(plan) == {"a.txt"}
    assert not any("objects" in str(f.path) for f in plan.files)
    assert all(".git" not in rel for rel in plan.dirs for rel in rel)


def test_a_symlinked_file_is_skipped(tmp_path):
    _tree(tmp_path, {"real.txt": b"x"})
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")

    plan = scan_folder(tmp_path)

    assert _names(plan) == {"real.txt"}
    assert any(why == SKIPPED_SYMLINK for _, why in plan.skipped)


def test_a_symlink_loop_does_not_hang_the_walk(tmp_path):
    """A link to its own parent is an infinite tree if it is followed."""
    _tree(tmp_path, {"sub/a.txt": b"x"})
    (tmp_path / "sub" / "loop").symlink_to(tmp_path, target_is_directory=True)

    plan = scan_folder(tmp_path)  # must simply return

    assert _names(plan) == {"a.txt"}
    assert any(why == SKIPPED_SYMLINK for _, why in plan.skipped)


def test_a_symlink_out_of_the_tree_is_not_followed(tmp_path):
    """Following it would upload files outside the folder the user picked."""
    outside = tmp_path.parent / "outside_tree"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_bytes(b"not yours")
    root = tmp_path / "chosen"
    root.mkdir()
    (root / "a.txt").write_bytes(b"x")
    (root / "escape").symlink_to(outside, target_is_directory=True)

    plan = scan_folder(root)

    assert _names(plan) == {"a.txt"}
    assert "secret.txt" not in {f.path.name for f in plan.files}


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_an_unreadable_directory_is_recorded_not_raised(tmp_path):
    _tree(tmp_path, {"good.txt": b"x", "locked/inside.txt": b"y"})
    locked = tmp_path / "locked"
    locked.chmod(0o000)
    try:
        plan = scan_folder(tmp_path)
        assert _names(plan) == {"good.txt"}
        assert any(why == SKIPPED_UNREADABLE for _, why in plan.skipped)
    finally:
        locked.chmod(0o755)


# -- ordering and totals -----------------------------------------------------


def test_entries_are_walked_in_a_stable_order(tmp_path):
    _tree(tmp_path, {"b.txt": b"x", "A.txt": b"x", "c.txt": b"x"})

    first = [f.path.name for f in scan_folder(tmp_path).files]
    second = [f.path.name for f in scan_folder(tmp_path).files]

    assert first == second
    assert first == ["A.txt", "b.txt", "c.txt"], "case-insensitive by name"


def test_total_bytes_counts_only_what_will_be_uploaded(tmp_path):
    _tree(tmp_path, {"a.bin": b"x" * 100, ".hidden": b"y" * 5000,
                     "sub/b.bin": b"z" * 50})

    plan = scan_folder(tmp_path)

    assert plan.total_bytes == 150, "the hidden file must not inflate the total"


# -- dependency directories --------------------------------------------------


def test_node_modules_is_skipped_by_default(tmp_path):
    """The case that turned a 69-file project into a 28,481-file upload."""
    _tree(tmp_path, {
        "src/app.js": b"mine",
        "package.json": b"{}",
        "node_modules/react/index.js": b"theirs",
        "node_modules/lodash/x.js": b"theirs",
    })

    plan = scan_folder(tmp_path)

    assert _names(plan) == {"app.js", "package.json"}
    assert any(why == SKIPPED_DEPENDENCY for _, why in plan.skipped)


def test_a_nested_node_modules_is_skipped_too(tmp_path):
    """frontend/node_modules held 4,439 files in the folder that prompted this."""
    _tree(tmp_path, {
        "frontend/src/a.js": b"mine",
        "frontend/node_modules/pkg/i.js": b"theirs",
    })

    plan = scan_folder(tmp_path)

    assert _names(plan) == {"a.js"}
    assert ("frontend", "node_modules") not in plan.dirs


def test_other_generated_directories_are_skipped(tmp_path):
    _tree(tmp_path, {
        "keep.txt": b"x",
        "__pycache__/m.pyc": b"x",
        "build/out.o": b"x",
        "dist/bundle.js": b"x",
        "target/debug/bin": b"x",
        "vendor/lib.go": b"x",
    })

    plan = scan_folder(tmp_path)

    assert _names(plan) == {"keep.txt"}


def test_dependencies_can_be_taken_deliberately(tmp_path):
    """Skipping is a default, not a rule the user cannot override."""
    _tree(tmp_path, {"a.js": b"x", "node_modules/pkg/i.js": b"y"})

    plan = scan_folder(tmp_path, skip_dependencies=False)

    assert _names(plan) == {"a.js", "i.js"}


def test_a_file_named_like_a_dependency_dir_is_still_uploaded(tmp_path):
    """Only directories are skipped; a file called 'build' is the user's."""
    _tree(tmp_path, {"build": b"a script, not a folder"})

    plan = scan_folder(tmp_path)

    assert _names(plan) == {"build"}


def test_the_skip_is_reported_so_it_is_never_silent(tmp_path):
    _tree(tmp_path, {"a.js": b"x", "node_modules/pkg/i.js": b"y"})

    plan = scan_folder(tmp_path)
    reasons = [(p.name, why) for p, why in plan.skipped]

    assert ("node_modules", SKIPPED_DEPENDENCY) in reasons
