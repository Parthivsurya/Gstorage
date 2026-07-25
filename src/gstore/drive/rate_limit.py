"""Quota-aware retry wrapper for Drive API calls.

Every Drive request is funneled through :func:`execute_with_backoff`, which
retries transient rate-limit / server errors with exponential backoff and
jitter. UNIT_COSTS documents the new per-project quota-unit model (list = 100,
download = 200, …) so budgeting is reasoned about in units, not request counts.
"""

from __future__ import annotations

import json
import random
import time
from typing import Callable, TypeVar

from googleapiclient.errors import HttpError

T = TypeVar("T")

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
    call: Callable[[], T], *, max_retries: int = 6, base_delay: float = 1.0
) -> T:
    """Run a Drive request callable, retrying transient rate-limit errors.

    `call` is a zero-arg callable performing exactly one request, e.g.
    ``lambda: request.execute()`` or a resumable upload's ``request.next_chunk``.
    """
    attempt = 0
    while True:
        try:
            return call()
        except HttpError as err:
            if attempt >= max_retries or not is_retryable(err):
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, 1)
            time.sleep(delay)
            attempt += 1
