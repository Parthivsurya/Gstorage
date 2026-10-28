"""Tests for the CA bundle behind stdlib HTTPS calls.

Shipped builds could not verify a single certificate: OpenSSL had no store to
fall back on, so every `urllib` call died with CERTIFICATE_VERIFY_FAILED. The
update check swallowed the error by design and nothing ever surfaced, so the
feature was dead in v0.1.0 for everyone while looking fine. The Google API
calls kept working throughout, because httplib2 carries its own bundle — which
is exactly why this hid for so long.
"""

from __future__ import annotations

import ssl

from gstore.tls import ssl_context


def test_the_context_actually_has_certificates():
    # The bug: ssl.create_default_context() loaded zero CAs here.
    assert len(ssl_context().get_ca_certs()) > 0


def test_verification_is_never_turned_off_to_make_it_work():
    # Silently not checking certificates would be worse than the visible
    # failure this replaced.
    context = ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_every_urlopen_in_the_app_passes_a_context():
    """Whoever adds the next urllib call inherits the fix.

    Discovered by walking the package rather than listing the files, because
    the failure mode is silent: a new caller that forgets the context works
    on a developer machine with a system CA store and dies in the shipped
    build, which is precisely how this went out in v0.1.0.
    """
    import ast
    from pathlib import Path

    import gstore

    offenders, checked = [], 0
    for path in sorted(Path(gstore.__file__).parent.rglob("*.py")):
        # encoding is explicit: Python source is UTF-8 by definition, but
        # read_text() defaults to the locale encoding, which is cp1252 on a
        # Windows runner. The em dashes and emoji in the UI modules made this
        # raise UnicodeDecodeError there and pass everywhere else.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(
                node.func, "id", None
            )
            if name != "urlopen":
                continue
            checked += 1
            if not any(kw.arg == "context" for kw in node.keywords):
                offenders.append(f"{path.name}:{node.lineno}")

    assert checked, "no urlopen calls found — has the update check moved?"
    assert not offenders, (
        "these urlopen calls have no SSL context and will fail in a packaged "
        f"build: {', '.join(offenders)}"
    )
