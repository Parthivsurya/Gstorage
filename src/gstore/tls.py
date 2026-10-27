"""A TLS context that actually trusts something.

Every HTTPS call this app makes through stdlib ``urllib`` was failing with
``CERTIFICATE_VERIFY_FAILED``, silently, in shipped builds:

    gstore.update: update check did not complete: URLError:
      <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify
       failed: unable to get local issuer certificate>

The cause is that OpenSSL has no certificate store to fall back on here —
``ssl.get_default_verify_paths()`` returns ``None`` for both cafile and
capath, and a default context loads zero CAs. A framework Python on macOS
needs *Install Certificates.command* run by hand, and a PyInstaller bundle
carries no system store at all, so the packaged app can never have one.

The Google API calls were unaffected and hid this: httplib2 ships its own CA
bundle and never consults OpenSSL's. Only the hand-rolled ``urllib`` paths —
the update check, and fetching a shared file — were broken, and the update
check swallows its own errors by design, so nothing ever surfaced.

certifi is the bundle httplib2 and google-auth already depend on, so this
adds no new install; it just points stdlib at it too.
"""

from __future__ import annotations

import ssl


def ssl_context() -> ssl.SSLContext:
    """A verifying context backed by certifi, falling back to the system store.

    Never returns an unverified context: a silent failure to check
    certificates would be far worse than the visible failure this replaces.
    """
    try:
        import certifi
    except ImportError:          # pragma: no cover - certifi is a hard dep
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())
