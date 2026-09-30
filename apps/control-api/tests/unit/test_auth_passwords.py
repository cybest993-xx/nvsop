from __future__ import annotations

from auth_fakes import FakeUsers

from factory_sop.auth.model import UserStatus
from factory_sop.auth.passwords import hash_password, verify_password


def test_a_password_verifies_against_its_own_hash() -> None:
    stored = hash_password("correct horse battery staple")

    assert (
        verify_password(
            password="correct horse battery staple",  # pragma: allowlist secret
            stored_hash=stored,
        )
        is True
    )


def test_a_wrong_password_does_not_verify() -> None:
    stored = hash_password("correct horse battery staple")

    assert (
        verify_password(
            password="Correct horse battery staple",  # pragma: allowlist secret
            stored_hash=stored,
        )
        is False
    )


def test_the_same_password_hashes_differently_every_time() -> None:
    # A per-hash salt. Equal hashes would let anyone holding the table see which accounts
    # share a password.
    assert hash_password("shift-lead-2026") != hash_password("shift-lead-2026")


def test_the_stored_hash_names_the_argon2id_variant() -> None:
    # §六 fixes Argon2id specifically. Argon2i and Argon2d are also valid encodings of this
    # shape, so the variant is asserted rather than assumed from the library default.
    stored = hash_password("shift-lead-2026")
    assert stored.startswith("$argon2id$v=19$m=65536,t=3,p=4$")  # pragma: allowlist secret


def test_prepared_accounts_share_only_the_real_hash() -> None:
    first, second = FakeUsers(), FakeUsers()
    password = "fixture-operator-2026"  # pragma: allowlist secret
    one = first.register(login_name="first", password=password)
    two = second.register(login_name="second", password=password)

    assert one.password_hash == two.password_hash
    assert verify_password(password=password, stored_hash=one.password_hash)
    assert not verify_password(
        password="different-password",  # pragma: allowlist secret
        stored_hash=one.password_hash,
    )
    first.deactivate(one.id)
    assert second.by_id[two.id].status is UserStatus.ACTIVE
    assert one.id != two.id


def test_a_stored_hash_that_is_not_an_argon2_encoding_does_not_verify() -> None:
    # A row damaged by hand, or one carrying some earlier scheme. It must read as "this
    # password is wrong", not raise out of the login use case.
    assert (
        verify_password(
            password="shift-lead-2026",  # pragma: allowlist secret
            stored_hash="plaintext",
        )
        is False
    )
