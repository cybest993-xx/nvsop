from __future__ import annotations

from factory_sop.auth.csrf import csrf_token_for, verify_csrf_token
from factory_sop.auth.tokens import SessionToken

SECRET = "deployment-csrf-secret"  # pragma: allowlist secret


def test_the_same_session_always_derives_the_same_csrf_token() -> None:
    # The token is not stored anywhere: it is recomputed from the session cookie on each
    # request, which is why `auth_session` needs no column for it.
    session = SessionToken.issue()

    assert csrf_token_for(session, secret=SECRET) == csrf_token_for(session, secret=SECRET)


def test_two_sessions_derive_different_csrf_tokens() -> None:
    # Bound to the session, so a token harvested from one login cannot be replayed against
    # another — which is what a plain random double-submit value would allow.
    assert csrf_token_for(SessionToken.issue(), secret=SECRET) != csrf_token_for(
        SessionToken.issue(), secret=SECRET
    )


def test_the_csrf_token_does_not_reveal_the_session_token() -> None:
    # It travels in a cookie the page's JavaScript can read, so it must not be the session
    # secret in another encoding.
    session = SessionToken.issue()

    assert session.value not in csrf_token_for(session, secret=SECRET)


def test_a_token_derived_under_another_secret_does_not_verify() -> None:
    # Without the secret, anyone able to set a cookie could also compute the header to match
    # it. The secret is what makes the pair unforgeable rather than merely self-consistent.
    session = SessionToken.issue()
    forged = csrf_token_for(session, secret="some other secret")

    assert verify_csrf_token(forged, session=session, secret=SECRET) is False


def test_the_matching_token_verifies() -> None:
    session = SessionToken.issue()

    assert (
        verify_csrf_token(csrf_token_for(session, secret=SECRET), session=session, secret=SECRET)
        is True
    )


def test_an_absent_token_does_not_verify() -> None:
    assert verify_csrf_token(None, session=SessionToken.issue(), secret=SECRET) is False


def test_a_token_belonging_to_another_session_does_not_verify() -> None:
    assert (
        verify_csrf_token(
            csrf_token_for(SessionToken.issue(), secret=SECRET),
            session=SessionToken.issue(),
            secret=SECRET,
        )
        is False
    )
