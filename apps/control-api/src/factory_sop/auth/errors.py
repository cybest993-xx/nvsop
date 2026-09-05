"""Why `auth` refused, in the vocabulary the HTTP adapter turns into `problem+json`.

§5.15 makes `error_code` a stable SCREAMING_SNAKE enumeration, and keeps it a different type
from `reason_code`: this one says an API call failed, that one is a judgment's domain reason.
Sharing a field name would let the front end handle 不可判定 as an HTTP failure.

An exception rather than a result union. Every refusal here has exactly one caller behavior —
do not proceed, report this code — and the alternative asks each of them to unpack a union
whose failure arm they can only forward.
"""

from __future__ import annotations

from enum import StrEnum
from typing import assert_never


class RefusalCode(StrEnum):
    """The `error_code` values `auth` produces."""

    # The login name and password together do not identify an account. Deliberately one code
    # for both halves: separate codes would make the login form an account enumerator.
    CREDENTIALS_REJECTED = "CREDENTIALS_REJECTED"

    # The password was right and the account is deactivated (`CONTEXT.md`: 停用). Told apart
    # from the above on purpose — an operator whose account was deactivated needs to know to
    # ask an administrator rather than to retype a password that is correct. It reveals a
    # status only to someone who has just proven they hold the credential.
    ACCOUNT_DEACTIVATED = "ACCOUNT_DEACTIVATED"

    # The request carried no session cookie at all.
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"

    # A session cookie arrived and is not usable: unknown, expired, revoked, or belonging to
    # an account that has since been deactivated. One code, because the states are not
    # reliably distinguishable — an expired row that the sweep has already removed is
    # indistinguishable from one that was revoked — and the client behavior is the same for
    # all of them: discard the cookie and log in again.
    SESSION_INVALID = "SESSION_INVALID"


class AuthenticationRefusedError(Exception):
    """`auth` declined to authenticate. Carries the code the response reports."""

    def __init__(self, code: RefusalCode) -> None:
        super().__init__(code.value)
        self.code = code


def refusal_problem(code: RefusalCode) -> tuple[int, str]:
    """The HTTP status and the Simplified Chinese title a refusal reports as `problem+json`.

    The mapping lives beside the codes it maps because a new `RefusalCode` and its wire
    meaning are one change, not two files. The `match` plus `assert_never` makes totality a
    type error rather than a runtime one: a code added without a mapping fails `mypy`, not
    the first caller who hits it. The title is what a client that does not recognize the
    `error_code` displays verbatim (§5.15's unknown-value fallback).

    Which refusals are "we do not know who you are" (401) and which are "we do, and no" (403)
    is the mapping's own decision: a deactivated account reached 403 only by presenting the
    correct password, so telling it apart is what lets the Web shell say 账户已停用 rather
    than 密码错误.
    """
    match code:
        case RefusalCode.CREDENTIALS_REJECTED:
            return 401, "登录名或密码不正确"
        case RefusalCode.ACCOUNT_DEACTIVATED:
            return 403, "账户已停用，请联系管理员"
        case RefusalCode.AUTHENTICATION_REQUIRED:
            return 401, "请先登录"
        case RefusalCode.SESSION_INVALID:
            return 401, "会话已失效，请重新登录"
        case _:
            assert_never(code)
