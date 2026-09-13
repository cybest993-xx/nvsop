"""The real connection probe: standard-library requests to the inference API identity endpoints.

Q31 requires the result to reflect actual requests: an unreachable endpoint is a failure, never
simulated success. The OpenAI-compatible ``/v1/models`` response and the base's ``/v1/metadata``
response together supply the model identities used for judgment provenance (§5.13). The center
stores those observed strings only; it does not create a model registry or distribution authority.
Credentials are rejected and failure text is scrubbed before it can reach the center's database,
logs, or API response (ADR-0008).
"""

from __future__ import annotations

import json
import re
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

from factory_sop.device.model import ConnectionState, carries_userinfo
from factory_sop.device.probing import ProbeReport

PROBE_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 1_048_576
MAX_DETAIL_LENGTH = 200

# A factory-floor connection test must go directly to the configured inference host. Honoring
# proxy environment variables would make the result depend on an unrelated center setting.
_DIRECT_OPENER = build_opener(ProxyHandler({}))


class UrllibConnectionProbe:
    """`ConnectionProbe` over `urllib`; it has no mutable state and asks the endpoint directly."""

    def probe(self, *, base_url: str) -> ProbeReport:
        """Request metadata and models, then report the endpoint's observed identities."""
        try:
            unsafe_url = carries_userinfo(base_url)
        except ValueError:
            return _failure("invalid endpoint URL")
        if unsafe_url:
            return _failure("endpoint URL carries credentials")

        root = base_url.rstrip("/")
        metadata_response = _fetch(f"{root}/v1/metadata")
        if isinstance(metadata_response, ProbeReport):
            return metadata_response
        metadata = _metadata_from(metadata_response)
        if metadata.status is ConnectionState.FAILURE:
            return metadata

        models_response = _fetch(f"{root}/v1/models")
        if isinstance(models_response, ProbeReport):
            return models_response
        models = _models_from(models_response)
        if models.status is ConnectionState.FAILURE:
            return models

        model_ids = tuple(dict.fromkeys(metadata.model_ids + models.model_ids))
        return ProbeReport(status=ConnectionState.SUCCESS, model_ids=model_ids)


def _fetch(url: str) -> bytes | ProbeReport:
    """Make one bounded direct request, turning transport failures into probe results."""
    try:
        with _DIRECT_OPENER.open(url, timeout=PROBE_TIMEOUT_SECONDS) as response:
            return cast("bytes", response.read(MAX_RESPONSE_BYTES))
    except HTTPError as error:
        return _failure(f"HTTP {error.code}")
    except (URLError, TimeoutError, ValueError, OSError) as error:
        return _failure(f"unreachable: {error}")


def _metadata_from(body: bytes) -> ProbeReport:
    """Turn the base metadata response into observed model identities."""
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _failure("response was not JSON")

    if not isinstance(document, dict) or not isinstance(document.get("modelInfo"), (dict, list)):
        return _failure("response was not metadata with model info")

    model_info = document["modelInfo"]
    entries = model_info.values() if isinstance(model_info, dict) else model_info
    model_ids = tuple(identity for entry in entries for identity in _metadata_identity(entry))
    if not model_ids:
        return _failure("metadata reported no model identities")
    return ProbeReport(status=ConnectionState.SUCCESS, model_ids=model_ids)


def _metadata_identity(entry: object) -> tuple[str, ...]:
    """Extract a non-empty model id from one metadata model description."""
    if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
        return (entry["id"],)
    if isinstance(entry, str) and entry:
        return (entry,)
    return ()


def _models_from(body: bytes) -> ProbeReport:
    """Turn an OpenAI-compatible model response into a verified report."""
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _failure("response was not JSON")

    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        return _failure("response was not a models list")

    model_ids = tuple(
        entry["id"]
        for entry in document["data"]
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]
    )
    if not model_ids:
        return _failure("endpoint reported no models")
    return ProbeReport(status=ConnectionState.SUCCESS, model_ids=model_ids)


def _failure(detail: str) -> ProbeReport:
    """Create a bounded failure report without retaining URL userinfo."""
    scrubbed = re.sub(r"//[^/@]*@", "//", detail)
    return ProbeReport(
        status=ConnectionState.FAILURE,
        detail=scrubbed[:MAX_DETAIL_LENGTH],
    )
