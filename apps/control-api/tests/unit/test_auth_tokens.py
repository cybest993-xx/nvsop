from __future__ import annotations

from factory_sop.auth.tokens import SessionToken, fingerprint


def test_a_token_is_not_stored_in_the_form_it_is_handed_out_in() -> None:
    # The cookie value reaches the browser; the table holds only its fingerprint, so a
    # dump of `auth_session` does not let anyone resume a session (§六: sessions are
    # revocable server-side records, not bearer material we keep a copy of).
    token = SessionToken.issue()

    assert fingerprint(token) != token.value


def test_a_fingerprint_identifies_the_token_it_was_taken_from() -> None:
    token = SessionToken.issue()

    assert fingerprint(token) == fingerprint(SessionToken(value=token.value))


def test_two_issued_tokens_differ() -> None:
    assert SessionToken.issue().value != SessionToken.issue().value


def test_an_issued_token_carries_at_least_256_bits() -> None:
    # Guessing rather than stealing must not be a way in. The value is URL-safe base64, so
    # each character carries 6 bits.
    assert len(SessionToken.issue().value) * 6 >= 256


def test_a_fingerprint_is_a_fixed_width_hex_digest() -> None:
    # The column is a fixed-width string, so a value of some other shape means the code
    # writing it changed rather than the row being long.
    assert len(fingerprint(SessionToken.issue())) == 64
