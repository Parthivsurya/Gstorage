"""What a share link points at, and how a share link is spelled.

A 20 GB file is 409 pieces, and naming them costs about 26 KB — far past what
anyone can paste into a chat. So the list of pieces is itself uploaded to
Drive as a small file, and the link names *that*:

    https://gstore.iampkb.dev/s#<manifest-id>.<key>

Two deliberate choices in that URL.

**It is a fragment, after the ``#``.** Fragments are never sent in an HTTP
request, so the page at gstore.iampkb.dev — a static GitHub Pages site — never
learns which file is being shared, and neither does anyone logging its
traffic. The page exists only to tell someone without Gstore what to install.
That keeps the promise the privacy policy makes: no server of ours sees
anything.

**The manifest is encrypted, and the key lives only in the link.** Drive file
ids are unguessable in practice but they are not secrets, so the manifest is
sealed with a key that exists nowhere except the link text. Someone who
somehow reaches the manifest file without the link gets ciphertext, and the
ids of the pieces stay unknown.

The pieces themselves are world-readable to anyone holding their ids, which is
what makes an unauthenticated download possible at all. Encrypting the
manifest protects the ids; it does not pretend to protect the bytes. Anyone
you send the link to can read the file, and can pass that ability on.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from cryptography.fernet import Fernet, InvalidToken

# Bumped only for a change the current reader cannot understand. A reader
# refuses a manifest newer than it knows rather than guessing.
FORMAT_VERSION = 1

# Trailing slash deliberately: the server 301s /s to /s/, and while browsers
# do carry a fragment across a redirect, a share link is forwarded through
# chat apps and mail clients that rewrite URLs. Not depending on it costs one
# character.
SHARE_BASE_URL = "https://gstore.iampkb.dev/s/"

# Drive ids and urlsafe-base64 both draw from [A-Za-z0-9_-], so the separator
# has to come from outside that alphabet or the two halves cannot be told
# apart.
_SEP = "."


class InvalidLink(ValueError):
    """The text given is not a share link this build can read."""


class InvalidManifest(ValueError):
    """The manifest could not be decrypted, parsed, or understood."""


@dataclass(frozen=True)
class Part:
    """One piece of the file: a Drive id, and what should come back."""

    drive_file_id: str
    md5: str
    size_bytes: int


@dataclass(frozen=True)
class Manifest:
    """Everything needed to rebuild a shared file from public pieces."""

    name: str
    size_bytes: int
    md5: str | None
    parts: tuple[Part, ...]

    @property
    def is_split(self) -> bool:
        """True if the file was spread over several accounts."""
        return len(self.parts) > 1

    def to_json(self) -> bytes:
        """Serialise compactly.

        Parallel arrays rather than a list of objects: at 409 pieces,
        repeating the three keys costs more than the ids themselves.
        """
        doc = {
            "v": FORMAT_VERSION,
            "name": self.name,
            "size": self.size_bytes,
            "md5": self.md5,
            "ids": [p.drive_file_id for p in self.parts],
            "md5s": [p.md5 for p in self.parts],
            "sizes": [p.size_bytes for p in self.parts],
        }
        return json.dumps(doc, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> Manifest:
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidManifest(f"not readable as a manifest: {exc}") from exc

        if not isinstance(doc, dict):
            raise InvalidManifest("manifest is not an object")

        version = doc.get("v")
        if version != FORMAT_VERSION:
            # Forward compatibility is the recipient's problem, and guessing
            # is worse than saying so: a newer sender may have changed what
            # the fields mean.
            raise InvalidManifest(
                f"manifest format {version!r} is newer than this version of "
                f"Gstore understands (expected {FORMAT_VERSION}). Update Gstore."
            )

        try:
            ids = list(doc["ids"])
            md5s = list(doc["md5s"])
            sizes = [int(s) for s in doc["sizes"]]
            name = str(doc["name"])
            size_bytes = int(doc["size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidManifest(f"manifest is missing or malformed: {exc}") from exc

        if not (len(ids) == len(md5s) == len(sizes)):
            raise InvalidManifest("manifest piece lists disagree in length")
        if not ids:
            raise InvalidManifest("manifest lists no pieces")

        total = sum(sizes)
        if total != size_bytes:
            raise InvalidManifest(
                f"manifest pieces total {total} bytes but the file is said to "
                f"be {size_bytes}"
            )

        parts = tuple(
            Part(drive_file_id=str(i), md5=str(m), size_bytes=int(s))
            for i, m, s in zip(ids, md5s, sizes)
        )
        return cls(name=name, size_bytes=size_bytes, md5=doc.get("md5"), parts=parts)

    def seal(self, key: bytes) -> bytes:
        """Encrypt for upload."""
        return Fernet(_fernet_key(key)).encrypt(self.to_json())

    @classmethod
    def unseal(cls, blob: bytes, key: bytes) -> Manifest:
        """Decrypt and parse what :meth:`seal` produced."""
        try:
            raw = Fernet(_fernet_key(key)).decrypt(blob)
        except (InvalidToken, ValueError) as exc:
            raise InvalidManifest(
                "the manifest could not be decrypted — the link may be "
                "truncated, mistyped, or for a different file"
            ) from exc
        return cls.from_json(raw)


def new_key() -> bytes:
    """A fresh 32-byte key for one share."""
    return os.urandom(32)


def _fernet_key(key: bytes) -> bytes:
    """Fernet wants urlsafe base64, the link carries raw bytes."""
    if len(key) != 32:
        raise ValueError(f"a share key is 32 bytes, got {len(key)}")
    return base64.urlsafe_b64encode(key)


def _b64(raw: bytes) -> str:
    """Unpadded urlsafe base64 — '=' in a URL fragment is noise."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encode_link(manifest_id: str, key: bytes) -> str:
    """The URL to hand someone."""
    if not manifest_id:
        raise ValueError("a share link needs a manifest id")
    return f"{SHARE_BASE_URL}#{manifest_id}{_SEP}{_b64(key)}"


def decode_link(text: str) -> tuple[str, bytes]:
    """Pull the manifest id and key out of a pasted link.

    Deliberately forgiving about what arrives: people paste the whole URL,
    sometimes only the part after the ``#``, sometimes with a stray space or
    angle brackets from a chat client wrapping it.
    """
    candidate = text.strip().strip("<>").strip()
    if not candidate:
        raise InvalidLink("no link given")

    if "#" in candidate:
        candidate = candidate.split("#", 1)[1]
    elif "://" in candidate:
        # A URL with no fragment cannot carry a share; saying which half is
        # missing is more use than "invalid link".
        raise InvalidLink(
            "that link has no '#' part — the half after it is what identifies "
            "the file, and some apps drop it when a link is forwarded"
        )

    candidate = unquote(candidate.split("?", 1)[0]).strip().strip("/")

    if _SEP not in candidate:
        raise InvalidLink("that does not look like a Gstore share link")

    manifest_id, _, key_text = candidate.partition(_SEP)
    if not manifest_id or not key_text:
        raise InvalidLink("the share link is incomplete")

    try:
        key = _unb64(key_text)
    except (ValueError, TypeError) as exc:
        raise InvalidLink("the key in that link is not readable") from exc

    if len(key) != 32:
        raise InvalidLink("the key in that link is the wrong length — it looks truncated")

    return manifest_id, key


def looks_like_link(text: str) -> bool:
    """Cheap test for offering to open something the user pasted."""
    try:
        decode_link(text)
    except (InvalidLink, ValueError):
        return False
    return True
