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
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import build_http

# googleapiclient's own default, kept deliberately.
#
# A shorter bound was tried (15 s) to notice a dead network sooner, but a
# resumable upload sends 5 MB per request: on a slow uplink a send can block
# longer than that, and every piece would then time out, retry, and time out
# again — an upload that never finishes. Responsiveness now comes from the
# retry/backoff in gstore.drive.rate_limit and from the Cancel button, not
# from a tight socket deadline.
TIMEOUT_SEC = 60


def drive_service(credentials: Credentials, timeout: int = TIMEOUT_SEC):
    """Build a Drive v3 service whose requests time out instead of hanging.

    The transport comes from googleapiclient's own ``build_http`` rather than a
    hand-made ``httplib2.Http``, because that constructor does one thing that is
    easy to miss and fatal to miss: it removes 308 from ``redirect_codes``.
    Drive answers every chunk of a resumable upload with *308 Resume
    Incomplete*, and a stock httplib2 treats 308 as a permanent redirect — then
    fails with "Redirected but the response is missing a Location: header."
    because a 308 from Drive carries no Location. Building our own transport
    broke every upload exactly that way.

    Only the timeout is overridden afterwards; httplib2 reads ``self.timeout``
    when it opens each connection, so setting it post-construction applies.
    """
    http = build_http()
    http.timeout = timeout
    authorized = google_auth_httplib2.AuthorizedHttp(credentials, http=http)
    return build("drive", "v3", http=authorized, cache_discovery=False)
