"""Quota-aware retry wrapper for Drive API calls.

Every Drive request is funneled through :func:`execute_with_backoff`, which
retries transient rate-limit / server errors with exponential backoff and
jitter. UNIT_COSTS documents the new per-project quota-unit model (list = 100,
download = 200, …) so budgeting is reasoned about in units, not request counts.
"""

from __future__ import annotations

import http.client
import json
import random
import socket
import ssl
import time
from typing import Callable, TypeVar

import httplib2
from googleapiclient.errors import HttpError

T = TypeVar("T")

# How long a cancel may wait on a backoff sleep before being noticed.
_SLEEP_SLICE = 0.25

# Reference Drive API quota-unit costs (post-May-2026 unit model).
UNIT_COSTS = {
    "about.get": 10,
    "files.list": 100,
    "files.get": 10,
    "files.create": 100,  # upload
    "files.download": 200,
    "files.delete": 10,
}

_RETRYABLE_STATUS = {429, 500, 502, 503}
_RETRYABLE_403_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}

# Transport-level failures worth retrying: a dropped link, a reset connection,
# DNS that briefly vanishes, a TLS record cut mid-stream. None of these are an
# HttpError, so before this they aborted a whole transfer outright — a blip a
# one-second wait would have survived.
#
# Retrying a resumable upload after one is safe: googleapiclient's next_chunk
# sets _in_error_state on any exception and re-queries the server for the true
# byte offset on the following call, so bytes are never sent twice or skipped.
RETRYABLE_NETWORK_ERRORS = (
    socket.timeout,  # == TimeoutError
    socket.gaierror,  # DNS lookup failed
    socket.herror,
    ConnectionError,  # reset / aborted / refused / broken pipe
    ssl.SSLError,
    http.client.HTTPException,  # IncompleteRead, BadStatusLine, ...
    httplib2.HttpLib2Error,  # ServerNotFoundError and friends
)

# A bad certificate will be just as bad in 60 seconds; failing fast beats six
# pointless retries. Listed first so it wins over ssl.SSLError above.
NON_RETRYABLE_NETWORK_ERRORS = (ssl.SSLCertVerificationError,)


class Cancelled(Exception):
    """Raised when `should_stop` asks an in-flight retry loop to give up."""


def _sleep(seconds: float, should_stop: Callable[[], bool] | None) -> None:
    """Sleep, but notice a cancel within _SLEEP_SLICE rather than after a minute."""
    if should_stop is None:
        time.sleep(seconds)
        return
    waited = 0.0
    while waited < seconds:
        if should_stop():
            raise Cancelled("cancelled while waiting to retry")
        time.sleep(min(_SLEEP_SLICE, seconds - waited))
        waited += _SLEEP_SLICE


def _error_reason(err: HttpError) -> str | None:
    try:
        data = json.loads(err.content.decode("utf-8"))
        errors = data.get("error", {}).get("errors") or []
        if errors:
            return errors[0].get("reason")
    except Exception:
        pass
    return None


def is_retryable(err: HttpError) -> bool:
    status = getattr(err, "status_code", None)
    if status is None and err.resp is not None:
        status = err.resp.status
    if status in _RETRYABLE_STATUS:
        return True
    if status == 403 and _error_reason(err) in _RETRYABLE_403_REASONS:
        return True
    return False


def execute_with_backoff(
    call: Callable[[], T],
    *,
    max_retries: int = 6,
    base_delay: float = 1.0,
    should_stop: Callable[[], bool] | None = None,
) -> T:
    """Run a Drive request callable, retrying transient failures with backoff.

    `call` is a zero-arg callable performing exactly one request, e.g.
    ``lambda: request.execute()`` or a resumable upload's ``request.next_chunk``.

    Retries both transient HTTP statuses (rate limits, 5xx) and transport
    errors (see RETRYABLE_NETWORK_ERRORS) — a dropped connection is the most
    common way a long upload dies, and it recovers on its own.

    `should_stop`, when given, is polled during the backoff sleep so a user
    cancel is not stuck behind a 32-second wait; it raises :class:`Cancelled`.
    """
    attempt = 0
    while True:
        if should_stop is not None and should_stop():
            raise Cancelled("cancelled before the request was made")
        try:
            return call()
        except NON_RETRYABLE_NETWORK_ERRORS:
            raise
        except (HttpError, *RETRYABLE_NETWORK_ERRORS) as err:
            transient = is_retryable(err) if isinstance(err, HttpError) else True
            if attempt >= max_retries or not transient:
                raise
        delay = base_delay * (2**attempt) + random.uniform(0, 1)
        _sleep(delay, should_stop)
        attempt += 1
