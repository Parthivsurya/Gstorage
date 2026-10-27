"""Sharing a stored file by link, without the recipient linking any account.

A shared file is read by someone who has no Gstore account, no Google account
and none of the sender's credentials. That is possible because each piece is
an ordinary Drive file the app created, and ``drive.file`` is enough to mark
one readable by "anyone with the link"; the recipient then fetches it with no
token at all.

Three parts:

* :mod:`gstore.share.manifest` — what a link points at, and how a link is
  spelled. Pure data; no network.
* :mod:`gstore.share.publish` — grant and revoke the public permission, and
  put the manifest where a recipient can reach it.
* :mod:`gstore.share.fetch` — read a shared file back, unauthenticated.
"""
