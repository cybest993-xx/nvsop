"""The permission enumeration itself: that it is closed, and shaped the way §5.15 fixes.

§5.15 fixes three things about a permission, and each is asserted here rather than left to
review: the name is exactly `module.resource.action`, the granularity is `view`/`edit`/
`delete`, and the value exists in code as an enum member rather than as a bare string. The
third is what the parser below is for — a caller holding a string has to come through it, and
an unregistered value is refused instead of silently granting nothing (or, worse, being
compared against by `==` somewhere and matching).
"""

from __future__ import annotations

import re

import pytest

from factory_sop.auth.permissions import (
    ACTIONS,
    Permission,
    UnregisteredPermissionError,
    parse_permission,
)


def test_every_permission_is_three_segments() -> None:
    for permission in Permission:
        assert len(permission.value.split(".")) == 3, permission


def test_every_permission_ends_in_view_edit_or_delete() -> None:
    # §5.15's granularity. A fourth verb — `approve`, `publish` — is the moment to reread that
    # section rather than to add a member: 发布 is deliberately folded into `edit` (Q18).
    for permission in Permission:
        assert permission.value.split(".")[2] in ACTIONS, permission


def test_a_permission_names_the_module_that_owns_the_resource() -> None:
    # The first segment is a backend module (§七). `auth`'s own permissions are all that exist
    # in this slice; each later module registers its own members in this same enum, which is
    # what keeps one closed set to check a role's contents against.
    for permission in Permission:
        assert permission.value.split(".")[0] == "auth", permission


def test_a_registered_permission_parses_to_its_member() -> None:
    assert parse_permission("auth.user.edit") is Permission.USER_EDIT


def test_an_unregistered_permission_is_refused_rather_than_ignored() -> None:
    # What a bare string would let through. A role stored with `auth.user.approve` in it would
    # otherwise be a role granting a permission nothing checks, and no layer would say so.
    with pytest.raises(UnregisteredPermissionError):
        parse_permission("auth.user.approve")


def test_a_well_shaped_but_unknown_module_is_refused_too() -> None:
    # Shape is not membership: `device.camera.edit` will be a real permission when `device`
    # exists, and until then a role naming it grants nothing.
    with pytest.raises(UnregisteredPermissionError):
        parse_permission("device.camera.edit")


def test_the_refusal_names_the_value_it_refused() -> None:
    # An administrator reading the message has to be able to tell which of the permissions they
    # submitted was the bad one.
    with pytest.raises(UnregisteredPermissionError, match=re.escape("auth.user.approve")):
        parse_permission("auth.user.approve")
