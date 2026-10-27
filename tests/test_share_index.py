"""Tests for the record of what is currently shared.

Withdrawing a share needs the manifest's id, and once the link has been handed
out that id exists nowhere else. Losing the row means the manifest can never
be deleted and the pieces stay world-readable with nothing pointing at the
fact — so these cover the paths that could drop or duplicate it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from gstore.db.database import Database
from gstore.index import IndexStore
from gstore.index.shares import Shares


@pytest.fixture
def db():
    database = Database(Path(tempfile.mkdtemp()) / "t.db").open().initialize()
    yield database
    database.close()


@pytest.fixture
def seeded(db):
    # Real accounts, because shares.manifest_account_id has a foreign key to
    # them -- recording a share against an account that does not exist is
    # exactly the inconsistency that constraint is there to stop.
    for account_id in (1, 3):
        db.conn.execute(
            "INSERT INTO accounts(id, email, status) VALUES(?,?,'active')",
            (account_id, f"acct{account_id}@example.com"),
        )
    db.conn.commit()

    index = IndexStore(db)
    first = index.create_file("a.bin", None, 10, None, None, md5="a" * 32)
    second = index.create_file("b.bin", None, 20, None, None, md5="b" * 32)
    return db, Shares(db), first, second


def test_a_share_round_trips(seeded):
    _, shares, file_id, _ = seeded
    shares.record(file_id, manifest_file_id="m-1", manifest_account_id=3,
                  link="https://x/s#m-1.k", part_count=7)

    row = shares.get(file_id)
    assert (row.manifest_file_id, row.manifest_account_id, row.part_count) == ("m-1", 3, 7)
    assert row.link.endswith("#m-1.k")


def test_an_unshared_file_has_no_row(seeded):
    _, shares, file_id, _ = seeded
    assert shares.get(file_id) is None


def test_resharing_replaces_rather_than_adding_a_second_manifest(seeded):
    # Two live manifests for one file would leave the older one unwithdrawable.
    _, shares, file_id, _ = seeded
    shares.record(file_id, manifest_file_id="m-1", manifest_account_id=1,
                  link="link-1", part_count=2)
    shares.record(file_id, manifest_file_id="m-2", manifest_account_id=3,
                  link="link-2", part_count=2)

    assert shares.get(file_id).manifest_file_id == "m-2"
    assert len(shares.list_all()) == 1


def test_clearing_removes_it(seeded):
    _, shares, file_id, _ = seeded
    shares.record(file_id, manifest_file_id="m-1", manifest_account_id=1,
                  link="l", part_count=1)
    shares.clear(file_id)
    assert shares.get(file_id) is None


def test_shared_ids_cover_only_what_is_shared(seeded):
    _, shares, first, second = seeded
    shares.record(first, manifest_file_id="m-1", manifest_account_id=1,
                  link="l", part_count=1)
    assert shares.shared_file_ids() == {first}
    assert second not in shares.shared_file_ids()


def test_deleting_the_file_takes_its_share_with_it(seeded):
    # Otherwise the row outlives the file and nothing can act on it.
    db, shares, file_id, _ = seeded
    shares.record(file_id, manifest_file_id="m-1", manifest_account_id=1,
                  link="l", part_count=1)
    db.conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
    db.conn.commit()
    assert shares.get(file_id) is None


def test_unlinking_the_account_keeps_the_share_recorded(seeded):
    # The manifest still exists on Drive; forgetting it would strand it.
    db, shares, file_id, _ = seeded
    db.conn.execute(
        "INSERT INTO accounts(id, email, status) VALUES(9, 'x@example.com', 'active')"
    )
    db.conn.commit()
    shares.record(file_id, manifest_file_id="m-1", manifest_account_id=9,
                  link="l", part_count=1)

    db.conn.execute("DELETE FROM accounts WHERE id = 9")
    db.conn.commit()

    row = shares.get(file_id)
    assert row is not None and row.manifest_file_id == "m-1"
    assert row.manifest_account_id is None
