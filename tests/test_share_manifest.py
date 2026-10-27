"""Tests for what a share link carries and how it is spelled.

A link is the whole of what a recipient has: get the parsing wrong and a
perfectly good share is unopenable, get the sealing wrong and the ids of
every piece are readable by anyone who stumbles on the manifest. Both halves
are pure data, so both are cheap to pin down exactly.
"""

from __future__ import annotations

import json

import pytest

from gstore.share.manifest import (
    FORMAT_VERSION,
    InvalidLink,
    InvalidManifest,
    Manifest,
    Part,
    decode_link,
    encode_link,
    looks_like_link,
    new_key,
)


def _manifest(parts: int = 3, size: int = 100) -> Manifest:
    return Manifest(
        name="imp files.rar",
        size_bytes=parts * size,
        md5="f" * 32,
        parts=tuple(
            Part(drive_file_id=f"drive-id-{i}", md5=f"{i:032d}", size_bytes=size)
            for i in range(parts)
        ),
    )


# -- the manifest ----------------------------------------------------------


def test_survives_a_round_trip_through_json():
    original = _manifest()
    assert Manifest.from_json(original.to_json()) == original


def test_survives_a_round_trip_through_encryption():
    original, key = _manifest(), new_key()
    assert Manifest.unseal(original.seal(key), key) == original


def test_a_single_piece_file_is_just_a_one_piece_manifest():
    whole = _manifest(parts=1)
    assert not whole.is_split
    assert Manifest.from_json(whole.to_json()) == whole


def test_the_wrong_key_does_not_open_it():
    sealed = _manifest().seal(new_key())
    with pytest.raises(InvalidManifest):
        Manifest.unseal(sealed, new_key())


def test_a_tampered_manifest_is_refused_rather_than_trusted():
    key = new_key()
    sealed = bytearray(_manifest().seal(key))
    sealed[-1] ^= 0x01
    with pytest.raises(InvalidManifest):
        Manifest.unseal(bytes(sealed), key)


def test_the_sealed_form_does_not_leak_the_piece_ids():
    # The whole point of sealing: the ids are what grant access to the bytes.
    sealed = _manifest().seal(new_key())
    assert b"drive-id-0" not in sealed


def test_a_newer_format_is_refused_with_advice_not_misread():
    doc = json.loads(_manifest().to_json())
    doc["v"] = FORMAT_VERSION + 1
    with pytest.raises(InvalidManifest, match="Update Gstore"):
        Manifest.from_json(json.dumps(doc).encode())


def test_piece_lists_of_different_lengths_are_rejected():
    doc = json.loads(_manifest().to_json())
    doc["md5s"].pop()
    with pytest.raises(InvalidManifest, match="disagree in length"):
        Manifest.from_json(json.dumps(doc).encode())


def test_pieces_that_do_not_add_up_to_the_file_are_rejected():
    # Catches a truncated or doctored manifest before anything is downloaded.
    doc = json.loads(_manifest().to_json())
    doc["size"] = 999999
    with pytest.raises(InvalidManifest, match="total"):
        Manifest.from_json(json.dumps(doc).encode())


def test_a_manifest_with_no_pieces_is_rejected():
    doc = json.loads(_manifest().to_json())
    doc.update(ids=[], md5s=[], sizes=[], size=0)
    with pytest.raises(InvalidManifest, match="no pieces"):
        Manifest.from_json(json.dumps(doc).encode())


def test_rubbish_is_refused_without_an_unhandled_error():
    with pytest.raises(InvalidManifest):
        Manifest.from_json(b"not json at all")


# -- the link --------------------------------------------------------------


def test_a_link_round_trips():
    key = new_key()
    assert decode_link(encode_link("1AbCdEf", key)) == ("1AbCdEf", key)


def test_the_key_rides_in_the_fragment_so_no_server_ever_sees_it():
    # The fragment is never sent in an HTTP request. That is the only reason
    # a static page can host these links without learning what is shared.
    link = encode_link("1AbCdEf", new_key())
    before_hash, _, after_hash = link.partition("#")
    assert "1AbCdEf" not in before_hash
    assert "1AbCdEf" in after_hash


def test_a_link_is_short_enough_to_paste_in_a_message():
    # A 33-character Drive id is the real case; 409 piece ids would not fit.
    assert len(encode_link("1" * 33, new_key())) < 140


def test_the_key_is_not_padded_with_noise():
    assert "=" not in encode_link("1AbCdEf", new_key())


@pytest.mark.parametrize(
    "mangle",
    [
        lambda link: f"  {link}  ",            # pasted with whitespace
        lambda link: f"<{link}>",              # a chat client wrapped it
        lambda link: link.partition("#")[2],   # only the fragment copied
        lambda link: link.replace("/s/#", "/s#"),   # the pre-redirect spelling
    ],
    ids=["whitespace", "angle-brackets", "fragment-only", "trailing-slash"],
)
def test_links_survive_the_usual_mangling_on_the_way_through_a_chat(mangle):
    key = new_key()
    assert decode_link(mangle(encode_link("1AbCdEf", key))) == ("1AbCdEf", key)


def test_a_link_that_lost_its_fragment_says_so_specifically():
    # Some apps strip the '#' half when forwarding; a generic "invalid link"
    # would send the user hunting in the wrong place.
    with pytest.raises(InvalidLink, match="no '#' part"):
        decode_link("https://gstore.iampkb.dev/s")


def test_a_truncated_key_is_caught_rather_than_failing_later_as_decryption():
    link = encode_link("1AbCdEf", new_key())
    with pytest.raises(InvalidLink, match="truncated"):
        decode_link(link[:-8])


@pytest.mark.parametrize(
    "text", ["", "   ", "hello", "https://example.com/", "no-separator-here"]
)
def test_things_that_are_not_links_are_rejected(text):
    with pytest.raises(InvalidLink):
        decode_link(text)


def test_looks_like_link_never_raises():
    # Used to decide whether to offer to open the clipboard, so it must be
    # safe to call on absolutely anything.
    assert looks_like_link(encode_link("1AbCdEf", new_key()))
    for text in ["", "hello", "https://example.com", "#", "a.b"]:
        assert looks_like_link(text) is False
