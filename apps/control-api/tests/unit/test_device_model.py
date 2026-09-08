"""The URL rule the module's input contracts enforce: no credentials in device URLs.

ADR-0008 and §5.12 keep credentials off the center entirely — not stored, not logged, not
echoed by the API. `carries_userinfo` is the one check every device URL passes through, so
what it counts as a credential is asserted here rather than left to the regex's mood.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from factory_sop.device.model import (
    ConnectorReachability,
    ConnectorTestResult,
    DeviceStatus,
    InferenceHost,
    carries_userinfo,
)


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


@pytest.mark.parametrize("credentials_configured", [False, None])
def test_a_non_rejected_result_must_confirm_credentials(
    credentials_configured: bool | None,
) -> None:
    with pytest.raises(ValueError, match="must confirm configured credentials"):
        ConnectorTestResult(
            reachability=ConnectorReachability.REACHABLE,
            credentials_configured=credentials_configured,
        )


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("recording_window_seconds", 0),
        ("recording_window_seconds", -1),
        ("disk_watermark_percent", 0),
        ("disk_watermark_percent", 100),
    ],
)
def test_the_domain_rejects_an_invalid_host_recording_configuration(field: str, value: int) -> None:
    recording_window_seconds = value if field == "recording_window_seconds" else 7 * 24 * 3600
    disk_watermark_percent = value if field == "disk_watermark_percent" else 85

    with pytest.raises(ValueError, match=field):
        InferenceHost(
            id=UUID(int=1),
            name="装配A线-推理机1",
            address="10.0.8.11",
            mediamtx_address=None,
            recording_window_seconds=recording_window_seconds,
            disk_watermark_percent=disk_watermark_percent,
            status=DeviceStatus.ACTIVE,
            revision=1,
            created_by=UUID(int=2),
            updated_by=UUID(int=2),
            created_at=datetime(2026, 9, 5, tzinfo=UTC),
            updated_at=datetime(2026, 9, 5, tzinfo=UTC),
        )
