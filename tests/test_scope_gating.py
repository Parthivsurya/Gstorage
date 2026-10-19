"""Tests for the Drive scope the app actually asks Google for.

This is the gate on public distribution: `drive.file` alone is not a restricted
scope, so anyone can sign in without Gstore holding a current CASA assessment,
whereas requesting `drive` unconditionally would put every public user behind an
"app not verified" wall. So the default must stay narrow, the widening must be
explicitly opt-in, and the legacy-token fallback must keep recognising both.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gstore import config
from gstore.auth.oauth import (
    BASE_SCOPES,
    DRIVE_FILE_SCOPE,
    DRIVE_FULL_SCOPE,
    FULL_DRIVE_SCOPES,
    SCOPES,
    requested_scopes,
)
from gstore.drive.browse import can_modify, has_full_read


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """Point config at a scratch app-data dir with no env override set."""
    monkeypatch.delenv(config.FULL_DRIVE_ENV, raising=False)
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    return tmp_path


def test_default_login_does_not_request_the_restricted_scope(isolated_data_dir):
    scopes = requested_scopes()
    assert scopes == [DRIVE_FILE_SCOPE]
    assert DRIVE_FULL_SCOPE not in scopes


def test_marker_file_opts_into_full_drive(isolated_data_dir):
    config.set_full_drive_requested(True)
    assert config.full_drive_requested() is True
    assert requested_scopes() == FULL_DRIVE_SCOPES


def test_opt_in_is_reversible(isolated_data_dir):
    config.set_full_drive_requested(True)
    config.set_full_drive_requested(False)
    assert config.full_drive_requested() is False
    assert requested_scopes() == BASE_SCOPES


def test_clearing_an_unset_opt_in_is_harmless(isolated_data_dir):
    config.set_full_drive_requested(False)  # no marker file exists yet
    assert config.full_drive_requested() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_env_var_enables_full_drive(value, isolated_data_dir, monkeypatch):
    monkeypatch.setenv(config.FULL_DRIVE_ENV, value)
    assert requested_scopes() == FULL_DRIVE_SCOPES


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_env_var_can_force_narrow_scope(value, isolated_data_dir, monkeypatch):
    # Env wins over the marker file, so a packaged build can be pinned narrow.
    config.set_full_drive_requested(True)
    monkeypatch.setenv(config.FULL_DRIVE_ENV, value)
    assert requested_scopes() == BASE_SCOPES


def test_requested_scopes_returns_a_fresh_list(isolated_data_dir):
    # Callers hand this to InstalledAppFlow; mutating it must not poison later
    # logins by permanently widening or narrowing the module-level constants.
    requested_scopes().append("https://example.invalid/injected")
    assert requested_scopes() == [DRIVE_FILE_SCOPE]
    assert BASE_SCOPES == [DRIVE_FILE_SCOPE]


def test_legacy_fallback_scopes_still_cover_both():
    # accounts.py falls back to SCOPES for token blobs saved before scopes were
    # recorded; narrowing it would make an old full-drive token refresh wrongly.
    assert DRIVE_FILE_SCOPE in SCOPES and DRIVE_FULL_SCOPE in SCOPES


def test_narrow_token_cannot_see_or_modify_whole_drive():
    creds = SimpleNamespace(scopes=[DRIVE_FILE_SCOPE])
    assert has_full_read(creds) is False
    assert can_modify(creds) is False


def test_opted_in_token_can_see_and_modify_whole_drive():
    creds = SimpleNamespace(scopes=FULL_DRIVE_SCOPES)
    assert has_full_read(creds) is True
    assert can_modify(creds) is True
