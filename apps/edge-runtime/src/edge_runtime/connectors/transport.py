"""The real HTTP exchange with a camera: `urllib`, digest auth, and nothing else.

Deliberately the thinnest thing that can satisfy `IsapiTransport`. Everything that decides
anything lives above it in `hikvision.py`, which is why that half is unit-tested on a bare CPU
and this half is not: what remains here is `urllib` behavior and one camera's actual answers,
which only a real device can verify. Gate I1 is where that happens
(`target-environment-validation-matrix.md`).

**Credentials.** They arrive as parameters and are held only in the password manager `urllib`
needs. Credentials are entered and encrypted on the inference host itself (ADR-0008); this
module reads no environment and no
file, and it puts no URL, header or credential into an exception message it returns — a
transport detail is triage text that reaches the operator's screen and the diagnostic log.

Standard library only (edge-autonomy.md §5.11).
"""

from __future__ import annotations

import urllib.error
import urllib.request
from urllib.parse import urljoin

from edge_runtime.connectors.hikvision import (
    BODY_LIMIT,
    Exchange,
    Response,
    TransportFailed,
    TransportRefused,
    TransportTimedOut,
)

XML_CONTENT_TYPE = "application/xml"


class UrllibIsapiTransport:
    """One camera's ISAPI endpoint, reached over HTTP with digest authentication.

    `base_url` carries the scheme, so a deployment that has HTTPS on its cameras uses it by
    configuration. Digest auth keeps the password off the wire either way; the request body is
    exposed on plain HTTP, which is a property of the factory network boundary the deployment
    accepts for media too (harness §3).
    """

    def __init__(self, *, base_url: str, username: str, password: str) -> None:
        self._base_url = base_url if base_url.endswith("/") else f"{base_url}/"
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, self._base_url, username, password)
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(manager),
            urllib.request.HTTPBasicAuthHandler(manager),
        )

    def exchange(
        self, method: str, path: str, /, *, body: str | None = None, timeout: float
    ) -> Exchange:
        """Perform one request, mapping every failure mode onto the four `Exchange` cases.

        The mapping is the whole point of this class, and one distinction in it matters
        physically: `HTTPError` means the device answered, so a write may have taken effect
        and the result is `TransportFailed`; a bare `URLError` means the request never got
        there, which is the only case a write may treat as "no relay moved".

        A digest handshake sends the request twice — the first attempt draws the 401 that
        carries the challenge. For a PUT that means the body is sent unauthenticated once.
        Whether a given firmware accepts that is a **premise pending gate I1**; if one rejects
        it, the repair is to prime the opener with a GET before the first write rather than to
        change anything above this seam.
        """
        request = urllib.request.Request(
            urljoin(self._base_url, path.lstrip("/")),
            data=None if body is None else body.encode("utf-8"),
            headers={} if body is None else {"Content-Type": XML_CONTENT_TYPE},
            method=method,
        )
        try:
            with self._opener.open(request, timeout=timeout) as answer:
                return Response(body=answer.read(BODY_LIMIT + 1).decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            # The device answered. `error.reason` is its status text; the body may carry an
            # ISAPI status code, and reading it here would mean reading an error body on the
            # physical-control path for no decision that depends on it.
            return TransportFailed(detail=f"{error.code} {error.reason}")
        except TimeoutError:
            return TransportTimedOut(after=timeout)
        except urllib.error.URLError as error:
            # No answer, and none was sent successfully either. `error.reason` is the socket
            # error; it names no credential.
            return TransportRefused(detail=str(error.reason))
