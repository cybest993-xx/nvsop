"""Identity, permissions and sessions for local accounts.

`auth` is the module every other slice's authorization goes through (§5.15). This first
slice owns the account and the session: who may log in, what a session's lifetime is, and
when one stops being usable. Roles and the permission enumeration arrive with #23.
"""
