"""Tests for what the browser offers once a file can be shared.

The dangerous state here is the UI and the database disagreeing. A file shown
as private while its pieces are public tells the user something false about
who can reach their data, and a Share button offered on a file whose account
is unlinked starts work that cannot finish. Both are checked against a real
FileBrowser over a real index.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from gstore.db.database import Database  # noqa: E402
from gstore.index import IndexStore  # noqa: E402
from gstore.share.manifest import encode_link, new_key  # noqa: E402
from gstore.ui.file_browser import FileBrowser  # noqa: E402
from gstore.ui.share_dialog import OpenShareDialog, ShareLinkDialog  # noqa: E402

MB = 1024**2


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def browser(qapp, tmp_path):
    db = Database(tmp_path / "idx.db").open().initialize()
    db.conn.execute(
        "INSERT INTO accounts(id, email, quota_limit_bytes, quota_used_bytes,"
        " quota_synced_at, status) VALUES(1,'a@example.com',?,?,datetime('now'),'active')",
        (15 * MB, 0),
    )
    db.conn.commit()
    view = FileBrowser(IndexStore(db))
    yield view
    db.close()


def _add_file(browser, name="f.bin", *, account_id=1):
    return browser.index.create_file(
        name, None, 4 * MB, account_id, f"drive-{name}", md5="a" * 32
    )


def _select_row(browser, index=0):
    browser.entries.setCurrentRow(index)


def test_sharing_is_offered_for_a_reachable_file(browser):
    _add_file(browser)
    browser.refresh()
    _select_row(browser)

    assert browser.share_button.isEnabled()
    assert browser.share_button.text() == "Share"


def test_sharing_is_not_offered_for_a_file_whose_account_is_gone(browser):
    # Its pieces cannot be made public, so offering the button would start
    # work that is certain to fail partway.
    browser.index.create_file("orphan.bin", None, 4 * MB, None, None, md5="b" * 32)
    browser.refresh()
    _select_row(browser)

    assert not browser.share_button.isEnabled()


def test_a_shared_file_says_so_on_the_button_and_in_the_list(browser):
    file_id = _add_file(browser)
    browser.shares.record(
        file_id, manifest_file_id="m-1", manifest_account_id=1,
        link=encode_link("m-1", new_key()), part_count=1,
    )
    browser.refresh()
    _select_row(browser)

    assert browser.share_button.text() == "Shared…"
    assert "🔗" in browser.entries.item(0).text()


def test_withdrawing_clears_the_marker(browser):
    file_id = _add_file(browser)
    browser.shares.record(
        file_id, manifest_file_id="m-1", manifest_account_id=1,
        link=encode_link("m-1", new_key()), part_count=1,
    )
    browser.refresh()
    assert "🔗" in browser.entries.item(0).text()

    browser.shares.clear(file_id)
    browser.refresh()

    assert "🔗" not in browser.entries.item(0).text()
    assert browser.share_button.text() == "Share"


def test_a_shared_file_can_still_be_reached_to_withdraw_it(browser):
    # Even once its account is unlinked: otherwise the only route to making
    # the pieces private again is gone.
    file_id = browser.index.create_file(
        "orphan.bin", None, 4 * MB, None, None, md5="b" * 32
    )
    browser.shares.record(
        file_id, manifest_file_id="m-1", manifest_account_id=None,
        link=encode_link("m-1", new_key()), part_count=1,
    )
    browser.refresh()
    _select_row(browser)

    assert browser.share_button.isEnabled()
    assert browser.share_button.text() == "Shared…"


def test_opening_a_link_stays_available_while_nothing_is_selected(browser):
    browser.refresh()
    assert browser.open_link_button.isEnabled()


def test_everything_share_related_is_disabled_while_busy(browser, monkeypatch):
    _add_file(browser)
    browser.refresh()
    _select_row(browser)
    monkeypatch.setattr(browser, "_busy", lambda: True)
    browser._update_buttons()

    assert not browser.share_button.isEnabled()
    assert not browser.open_link_button.isEnabled()


# -- the dialogs -----------------------------------------------------------


def test_the_open_dialog_refuses_to_proceed_on_a_mangled_link(qapp):
    dialog = OpenShareDialog()
    for text in ["", "hello", "https://example.com", "https://gstore.iampkb.dev/s"]:
        dialog.link_field.setText(text)
        assert not dialog.ok.isEnabled(), text


def test_the_open_dialog_accepts_a_real_link(qapp):
    dialog = OpenShareDialog()
    dialog.link_field.setText(encode_link("1AbCdEf", new_key()))
    assert dialog.ok.isEnabled()


def test_copying_puts_the_whole_link_on_the_clipboard(qapp):
    from PySide6.QtGui import QGuiApplication

    link = encode_link("1AbCdEf", new_key())
    dialog = ShareLinkDialog("f.bin", link, 3, "2026-10-28")
    dialog._copy()

    assert QGuiApplication.clipboard().text() == link


def test_the_link_shown_is_never_editable(qapp):
    # It is a key; a stray keystroke would hand out one that does not work.
    link = encode_link("1AbCdEf", new_key())
    assert ShareLinkDialog("f.bin", link, 1).link_field.isReadOnly()
