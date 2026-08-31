from __future__ import annotations

from factory_sop.auth.passwords import hash_password, verify_password


def test_a_password_verifies_against_its_own_hash() -> None:
    stored = hash_password("correct horse battery staple")

    assert verify_password(password="correct horse battery staple", stored_hash=stored) is True


def test_a_wrong_password_does_not_verify() -> None:
    stored = hash_password("correct horse battery staple")

    assert verify_password(password="Correct horse battery staple", stored_hash=stored) is False


def test_the_same_password_hashes_differently_every_time() -> None:
    # A per-hash salt. Equal hashes would let anyone holding the table see which accounts
    # share a password.
    assert hash_password("shift-lead-2026") != hash_password("shift-lead-2026")


def test_the_stored_hash_names_the_argon2id_variant() -> None:
    # §六 fixes Argon2id specifically. Argon2i and Argon2d are also valid encodings of this
    # shape, so the variant is asserted rather than assumed from the library default.
    assert hash_password("shift-lead-2026").startswith("$argon2id$")


def test_a_stored_hash_that_is_not_an_argon2_encoding_does_not_verify() -> None:
    # A row damaged by hand, or one carrying some earlier scheme. It must read as "this
    # password is wrong", not raise out of the login use case.
    assert verify_password(password="shift-lead-2026", stored_hash="plaintext") is False
