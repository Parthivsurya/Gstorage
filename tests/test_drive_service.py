"""Tests for the Drive service transport (gstore.drive.service).

Every Drive call is built here, so a mistake in this one function breaks all of
them at once — and it did. Bounding the request timeout by hand-building an
``httplib2.Http`` silently reintroduced 308 into ``redirect_codes``, which
googleapiclient's own ``build_http`` removes on purpose: Drive answers every
chunk of a resumable upload with *308 Resume Incomplete*, and httplib2 treats a
308 as a permanent redirect. Since Drive's 308 carries no ``Location`` header,
every upload died with "Redirected but the response is missing a Location:
header."

These pin the two properties the transport must have.
"""

from __future__ import annotations

from google.oauth2.credentials import Credentials

from gstore.drive.service import TIMEOUT_SEC, drive_service


def _creds() -> Credentials:
    return Credentials(token="not-a-real-token")


def test_308_is_not_treated_as_a_redirect():
    """Regression: Drive uses 308 for resumable uploads, not for redirects."""
    http = drive_service(_creds())._http.http
    assert 308 not in http.redirect_codes


def test_ordinary_redirects_are_still_followed():
    """Only 308 is special — don't disable redirect handling wholesale."""
    http = drive_service(_creds())._http.http
    assert {301, 302, 303, 307} <= http.redirect_codes


def test_requests_have_a_bounded_timeout():
    """A request must not be able to block forever on a dead socket."""
    http = drive_service(_creds())._http.http
    assert http.timeout == TIMEOUT_SEC


def test_timeout_is_generous_enough_for_a_5mb_upload_piece():
    """A tight bound starves slow uplinks.

    A resumable upload sends 5 MB per request. At 15 s that needs ~2.7 Mbps
    upstream or every piece times out, retries and times out again — an upload
    that never finishes. Responsiveness comes from retry/backoff and Cancel,
    not from a short socket deadline.
    """
    assert TIMEOUT_SEC >= 60


def test_the_transport_reaches_the_request_object():
    """A service-level http is useless if the upload request doesn't inherit it."""
    service = drive_service(_creds())
    request = service.files().create(body={"name": "x"}, fields="id")
    http = request.http.http
    assert http.timeout == TIMEOUT_SEC
    assert 308 not in http.redirect_codes


def test_timeout_is_overridable():
    http = drive_service(_creds(), timeout=42)._http.http
    assert http.timeout == 42
    assert 308 not in http.redirect_codes  # still correct at a custom timeout
