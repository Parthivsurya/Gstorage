"""Drive API service construction with a bounded network timeout.

google-api-python-client defaults to a 60-second socket timeout. That is why a
dropped Wi-Fi link used to freeze a transfer for a full minute before it even
admitted failure — long enough that the app looks hung rather than busy. Every
Drive service is built here so one timeout applies everywhere.

The timeout is set on this service's own http object rather than with
``socket.setdefaulttimeout()``. The global version would also apply to the
OAuth loopback server, which waits for a human to finish consenting in a
browser and must be allowed to block for minutes.
"""

from __future__ import annotations

import google_auth_httplib2
import httplib2
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# Long enough to ride out a slow response on a healthy link, short enough that
# a dead one is noticed quickly. Each attempt is retried with exponential
# backoff — see gstore.drive.rate_limit — so this is a per-try bound, not a
# deadline for the whole operation.
TIMEOUT_SEC = 15


def drive_service(credentials: Credentials, timeout: int = TIMEOUT_SEC):
    """Build a Drive v3 service whose requests time out instead of hanging."""
    authorized = google_auth_httplib2.AuthorizedHttp(
        credentials, http=httplib2.Http(timeout=timeout)
    )
    return build("drive", "v3", http=authorized, cache_discovery=False)
