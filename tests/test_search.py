"""Tests for the flat, tree-spanning file search (IndexStore.search_files).

Search backs the Files tab's unified "search all files" box: it must find
matches regardless of which folder — or which physical account — a file lives
on, match case-insensitively, and treat LIKE wildcards in the term literally so
a user searching for "50%" doesn't get a wildcard match.
"""

from __future__ import annotations

from gstore.db.database import Database
from gstore.index import IndexStore


def _store(tmp_path) -> IndexStore:
    db = Database(tmp_path / "search.db").open().initialize()
    return IndexStore(db)


def _new_file(store: IndexStore, name: str, parent_id=None) -> int:
    return store.create_file(
        name, parent_id, size_bytes=1, home_account_id=None, drive_file_id=None
    )


def test_search_spans_folders(tmp_path):
    store = _store(tmp_path)
    photos = store.create_folder("Photos")
    nested = store.create_folder("2024", photos)
    _new_file(store, "beach.jpg", photos)
    _new_file(store, "sunset.jpg", nested)
    _new_file(store, "notes.txt")  # virtual root

    hits = {f.name for f in store.search_files("jpg")}
    assert hits == {"beach.jpg", "sunset.jpg"}


def test_search_is_case_insensitive_substring(tmp_path):
    store = _store(tmp_path)
    _new_file(store, "Report_FINAL.pdf")
    assert [f.name for f in store.search_files("final")] == ["Report_FINAL.pdf"]
    assert [f.name for f in store.search_files("report_final.pdf")] == [
        "Report_FINAL.pdf"
    ]


def test_search_matches_wildcards_literally(tmp_path):
    store = _store(tmp_path)
    _new_file(store, "battery 50% left.log")
    _new_file(store, "battery full.log")

    # "50%" must not behave like a LIKE wildcard and match everything.
    assert [f.name for f in store.search_files("50%")] == ["battery 50% left.log"]


def test_blank_query_returns_nothing(tmp_path):
    store = _store(tmp_path)
    _new_file(store, "anything.txt")
    assert store.search_files("") == []
    assert store.search_files("   ") == []
