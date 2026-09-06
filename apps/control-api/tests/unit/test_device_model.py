"""The URL rule the module's input contracts enforce: no credentials in device URLs.

ADR-0008 and §5.12 keep credentials off the center entirely — not stored, not logged, not
echoed by the API. `carries_userinfo` is the one check every device URL passes through, so
what it counts as a credential is asserted here rather than left to the regex's mood.
"""

from __future__ import annotations

import pytest

from factory_sop.device.model import carries_userinfo


@pytest.mark.parametrize(
    "url",
    [
        "http://user:secret@10.0.8.11:8000",  # pragma: allowlist secret
        "https://user@10.0.8.11",  # a bare name is still a credential
        "http://:secret@10.0.8.11",  # pragma: allowlist secret
    ],
)
def test_a_url_with_a_userinfo_segment_is_reported(url: str) -> None:
    assert carries_userinfo(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.8.11:8000",
        "https://mediamtx.factory.internal:8888",
        "http://10.0.8.11:8000/@weird/path",  # an @ in the path is not a userinfo segment
    ],
)
def test_a_plain_url_is_not_reported(url: str) -> None:
    assert not carries_userinfo(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.8.11:8000?access_token=fixture-marker",
        "http://10.0.8.11:8000#access_token=fixture-marker",
        "http://10.0.8.11:8000?",
        "http://10.0.8.11:8000#",
    ],
)
def test_an_endpoint_with_a_query_or_fragment_is_reported_as_unsafe(url: str) -> None:
    assert carries_userinfo(url)
