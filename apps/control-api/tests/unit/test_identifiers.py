from __future__ import annotations

from datetime import UTC, datetime

from factory_sop.identifiers import new_id


def test_the_identifier_declares_version_7() -> None:
    assert new_id().version == 7


def test_the_identifier_carries_the_rfc_9562_variant_bits() -> None:
    # RFC 9562 §4.1: the two most significant bits of octet 8 are 0b10.
    assert new_id().bytes[8] >> 6 == 0b10


def test_the_leading_48_bits_are_the_millisecond_the_identifier_was_made() -> None:
    # RFC 9562 §5.7 puts big-endian Unix milliseconds there. Decoding them back is what
    # makes a v7 identifier sort by creation time and cluster in an index, so it is the one
    # property worth pinning rather than trusting.
    before = datetime.now(UTC)
    identifier = new_id()
    after = datetime.now(UTC)

    encoded_ms = int.from_bytes(identifier.bytes[:6], "big")

    assert before.timestamp() * 1000 - 1 <= encoded_ms <= after.timestamp() * 1000 + 1


def test_two_identifiers_made_in_the_same_millisecond_differ() -> None:
    assert len({new_id() for _ in range(1000)}) == 1000
