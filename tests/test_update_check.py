"""Tests for the update check (gstore.update, gstore.ui.update_worker).

This is the only request Gstore makes to anywhere other than Google, so it
has to behave: never nag about a version that is not newer, never surface a
failure the user cannot act on, never run at all once switched off, and never
send anything but an anonymous GET.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error

import pytest

from gstore import config, update
from gstore.update import Release, check_for_update, is_newer, parse_version


# -- version comparison ------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("v0.0.5", (0, 0, 5)),
        ("0.0.5", (0, 0, 5)),
        ("v1.2", (1, 2, 0)),
        ("v3", (3, 0, 0)),
        ("Gstore v2.10.1", (2, 10, 1)),
        ("", None),
        ("nightly", None),
    ],
)
def test_parse_version(text, expected):
    assert parse_version(text) == expected


@pytest.mark.parametrize(
    "candidate,current,expected",
    [
        ("v0.0.6", "0.0.5", True),
        ("v0.1.0", "0.0.9", True),
        ("v1.0.0", "0.9.9", True),
        ("v0.0.5", "0.0.5", False),   # same version must not nag
        ("v0.0.4", "0.0.5", False),   # older must not nag
        ("v0.0.10", "0.0.9", True),   # numeric, not lexical
        ("garbage", "0.0.5", False),  # unparseable is never "newer"
        ("v0.0.6", "garbage", False),
    ],
)
def test_is_newer(candidate, current, expected):
    assert is_newer(candidate, current) is expected


# -- what the check does with GitHub's answer --------------------------------


def _payload(**kw):
    base = {
        "tag_name": "v0.1.0",
        "html_url": "https://github.com/Parthivsurya/Gstorage/releases/tag/v0.1.0",
        "body": "Faster transfers.",
        "draft": False,
        "prerelease": False,
    }
    base.update(kw)
    return base


@pytest.fixture
def github(monkeypatch):
    state = {"payload": _payload(), "raises": None, "calls": 0}

    def fake_fetch(timeout=update.TIMEOUT_SEC):
        state["calls"] += 1
        if state["raises"]:
            raise state["raises"]
        return state["payload"]

    monkeypatch.setattr(update, "fetch_latest", fake_fetch)
    return state


def test_reports_a_newer_release(github):
    release = check_for_update("0.0.5")
    assert isinstance(release, Release)
    assert release.version == "0.1.0"
    assert release.notes == "Faster transfers."


def test_says_nothing_when_up_to_date(github):
    github["payload"] = _payload(tag_name="v0.0.5")
    assert check_for_update("0.0.5") is None


def test_says_nothing_when_the_release_is_older(github):
    github["payload"] = _payload(tag_name="v0.0.1")
    assert check_for_update("0.0.5") is None


def test_ignores_drafts_and_prereleases(github):
    github["payload"] = _payload(draft=True)
    assert check_for_update("0.0.5") is None
    github["payload"] = _payload(draft=False, prerelease=True)
    assert check_for_update("0.0.5") is None


def test_a_failed_check_is_silent(github):
    """Offline is not an error the user can act on."""
    github["payload"] = None
    assert check_for_update("0.0.5") is None


def test_falls_back_to_the_releases_page_without_a_url(github):
    github["payload"] = _payload(html_url=None)
    release = check_for_update("0.0.5")
    assert release is not None and release.url == update.RELEASES_PAGE


def test_missing_release_notes_do_not_break_it(github):
    github["payload"] = _payload(body=None)
    release = check_for_update("0.0.5")
    assert release is not None and release.notes == ""


# -- network failures --------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("offline"),
        socket.timeout("timed out"),
        OSError("no route to host"),
        ValueError("not json"),
    ],
)
def test_fetch_swallows_every_transport_failure(monkeypatch, error):
    def boom(*a, **kw):
        raise error

    monkeypatch.setattr(update.urllib.request, "urlopen", boom)
    assert update.fetch_latest(timeout=1) is None


def test_the_request_is_anonymous(monkeypatch):
    """No account, no tokens, no identifier — only a plain GET."""
    captured = {}

    class FakeResponse:
        def read(self):
            return json.dumps(_payload()).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None, context=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["data"] = request.data
        captured["method"] = request.get_method()
        captured["context"] = context
        return FakeResponse()

    monkeypatch.setattr(update.urllib.request, "urlopen", fake_urlopen)
    update.fetch_latest()

    assert captured["method"] == "GET"
    assert captured["data"] is None  # nothing is sent in a body
    assert captured["url"] == update.RELEASES_API
    sent = " ".join(f"{k}:{v}" for k, v in captured["headers"].items()).lower()
    for forbidden in ("authorization", "cookie", "token", "@"):
        assert forbidden not in sent


def test_the_request_verifies_the_certificate(monkeypatch):
    """Anonymous must not mean unverified.

    Shipped builds had no CA store at all, so this call failed every time and
    the feature was dead in v0.1.0 without ever saying so. See gstore.tls.
    """
    import ssl

    captured = {}

    class FakeResponse:
        def read(self):
            return json.dumps(_payload()).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None, context=None):
        captured["context"] = context
        return FakeResponse()

    monkeypatch.setattr(update.urllib.request, "urlopen", fake_urlopen)
    update.fetch_latest()

    context = captured["context"]
    assert context is not None, "urlopen was called without an SSL context"
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert len(context.get_ca_certs()) > 0, "the context trusts no CAs"


# -- the switch --------------------------------------------------------------


@pytest.fixture
def app_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    monkeypatch.delenv(config.UPDATE_CHECK_ENV, raising=False)
    return tmp_path


def test_checking_is_on_by_default(app_data):
    assert config.update_check_enabled() is True


def test_it_can_be_switched_off_and_back_on(app_data):
    config.set_update_check_enabled(False)
    assert config.update_check_enabled() is False
    config.set_update_check_enabled(True)
    assert config.update_check_enabled() is True


def test_the_environment_can_disable_it(app_data, monkeypatch):
    monkeypatch.setenv(config.UPDATE_CHECK_ENV, "1")
    assert config.update_check_enabled() is False


def test_a_skipped_version_is_remembered(app_data):
    assert config.skipped_versions() == set()
    config.skip_version("0.1.0")
    assert "0.1.0" in config.skipped_versions()
    config.skip_version("0.2.0")
    assert config.skipped_versions() == {"0.1.0", "0.2.0"}


def test_the_worker_stays_quiet_when_switched_off(app_data, monkeypatch):
    os.environ[config.UPDATE_CHECK_ENV] = "1"
    try:
        from gstore.ui.update_worker import UpdateWorker

        called = {"n": 0}
        monkeypatch.setattr(
            "gstore.ui.update_worker.check_for_update",
            lambda *a, **kw: called.__setitem__("n", called["n"] + 1),
        )
        worker = UpdateWorker("0.0.5")
        worker.run()  # run() directly: no thread needed to prove the guard
        assert called["n"] == 0
    finally:
        del os.environ[config.UPDATE_CHECK_ENV]
