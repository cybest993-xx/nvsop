"""主机公钥请求身份契约的测试。"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from random import Random
from typing import ClassVar
from unittest.mock import patch

from nvsop_contracts import (
    HostIdentityKeyPair,
    HostIdentityRequest,
    generate_host_identity_key_pair,
    sign_host_identity_request,
    validate_host_identity_private_key,
    verify_host_identity_request,
)


def _fixture_host_identity(seed: int) -> HostIdentityKeyPair:
    """用固定伪随机流生成合成测试密钥, 避免测试依赖系统熵。"""
    random = Random(seed)
    with (
        patch("nvsop_contracts.host_identity.secrets.randbits", side_effect=random.getrandbits),
        patch("nvsop_contracts.host_identity.secrets.randbelow", side_effect=random.randrange),
    ):
        return generate_host_identity_key_pair()


class HostIdentityContractTest(unittest.TestCase):
    key_pair: ClassVar[HostIdentityKeyPair]
    request: ClassVar[HostIdentityRequest]

    @classmethod
    def setUpClass(cls) -> None:
        cls.key_pair = _fixture_host_identity(5)
        cls.request = HostIdentityRequest(
            method="POST",
            path="/api/v1/device-commands/00000000-0000-0000-0000-000000000001/result",
            host_id="00000000-0000-0000-0000-000000000002",
            timestamp=1_800_000_000,
            nonce="request-nonce-1",
            body={
                "outcome": "reachable",
                "detail": None,
                "credentials_configured": True,
                "failure_code": None,
            },
        )

    def test_a_private_key_signature_is_verified_by_the_public_key(self) -> None:
        signature = sign_host_identity_request(self.request, private_key=self.key_pair.private_key)

        self.assertTrue(
            verify_host_identity_request(
                self.request,
                public_key=self.key_pair.public_key,
                signature=signature,
            )
        )

    def test_a_signature_is_bound_to_every_request_identity_field(self) -> None:
        signature = sign_host_identity_request(self.request, private_key=self.key_pair.private_key)

        for changed in (
            replace(self.request, method="GET"),
            replace(self.request, path="/api/v1/device-commands/next"),
            replace(self.request, host_id="other-host"),
            replace(self.request, timestamp=self.request.timestamp + 1),
            replace(self.request, nonce="other-nonce"),
            replace(self.request, body={"outcome": "unreachable"}),
        ):
            with self.subTest(changed=changed):
                self.assertFalse(
                    verify_host_identity_request(
                        changed,
                        public_key=self.key_pair.public_key,
                        signature=signature,
                    )
                )

    def test_a_private_key_must_match_its_public_key(self) -> None:
        private_key = json.loads(self.key_pair.private_key)
        private_key["private_exponent"] = "Ag"
        with self.assertRaisesRegex(ValueError, "私钥与公钥不匹配"):
            validate_host_identity_private_key(json.dumps(private_key))

    def test_public_key_does_not_contain_the_private_exponent(self) -> None:
        self.assertNotIn("private_exponent", self.key_pair.public_key)

    def test_a_nonce_longer_than_the_storage_bound_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "随机数过长"):
            replace(self.request, nonce="n" * 129)

    def test_a_malformed_signature_is_rejected(self) -> None:
        self.assertFalse(
            verify_host_identity_request(
                self.request,
                public_key=self.key_pair.public_key,
                signature="not-a-signature",
            )
        )


if __name__ == "__main__":
    unittest.main()
