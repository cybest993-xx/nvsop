"""录像压缩的会话权限、主机签名与真实来源观测边界。"""

from test_device_command_routes import Center

from factory_sop.auth.api import Permission

HOSTS = "/api/v1/inference-hosts"
EDGE = "/api/v1/device-commands/compression"


def test_control_is_queued_then_only_signed_owner_observation_completes_it() -> None:
    center = Center()
    center.granted = frozenset({Permission.INFERENCE_HOST_VIEW, Permission.INFERENCE_HOST_EDIT})
    host = center.hosts.register(name="compression-host")
    path = f"{HOSTS}/{host.id}/recording-compression/commands"
    queued = center.client.post(
        path,
        json={"action": "pause"},
        headers=center.operator_headers(**{"Idempotency-Key": "pause-1"}),
    )
    assert queued.status_code == 202, queued.text
    command_id = queued.json()["id"]
    assert queued.json()["status"] == "pending"
    assert queued.json()["observation"] is None
    assert (
        center.client.post(
            path,
            json={"action": "pause"},
            headers=center.operator_headers(**{"Idempotency-Key": "pause-1"}),
        ).json()["id"]
        == command_id
    )
    claim = center.edge.get(
        f"{EDGE}/next", headers=center.edge_headers(host.id, method="GET", path=f"{EDGE}/next")
    )
    assert claim.status_code == 200, claim.text
    result_path = f"{EDGE}/{command_id}/result"
    result = {
        "state": "paused",
        "requested": "pause",
        "segment": "segment.mp4",
        "pid": 123,
        "readrate": 0.5,
        "failure_code": None,
        "detail": None,
    }
    other = center.hosts.register(name="other-host")
    wrong_host = center.edge.post(
        result_path,
        json=result,
        headers=center.edge_headers(
            other.id,
            method="POST",
            path=result_path,
            body=result,
            **{"X-Command-Claim-Token": claim.json()["claim_token"]},
        ),
    )
    assert wrong_host.status_code == 403
    invalid_token = center.edge.post(
        result_path,
        json=result,
        headers=center.edge_headers(
            host.id,
            method="POST",
            path=result_path,
            body=result,
            **{"X-Command-Claim-Token": "invalid-token"},
        ),
    )
    assert invalid_token.status_code == 409
    assert (
        center.client.post(
            path, json={"action": "abort"}, headers={"Idempotency-Key": "without-csrf"}
        ).status_code
        == 403
    )
    # 收到 pause 请求不等于内核已停止; 未确认的成功回报必须拒绝。
    unconfirmed = {**result, "state": "pausing"}
    assert (
        center.edge.post(
            result_path,
            json=unconfirmed,
            headers=center.edge_headers(
                host.id,
                method="POST",
                path=result_path,
                body=unconfirmed,
                **{"X-Command-Claim-Token": claim.json()["claim_token"]},
            ),
        ).status_code
        == 409
    )
    posted = center.edge.post(
        result_path,
        json=result,
        headers=center.edge_headers(
            host.id,
            method="POST",
            path=result_path,
            body=result,
            **{"X-Command-Claim-Token": claim.json()["claim_token"]},
        ),
    )
    assert posted.status_code == 200, posted.text
    observed = center.client.get(f"{path}/{command_id}")
    assert observed.status_code == 200
    assert observed.json()["status"] == "succeeded"
    assert observed.json()["observation"]["state"] == "paused"
    assert "claim_token" not in observed.json()
    center.granted = frozenset({Permission.INFERENCE_HOST_VIEW})
    assert (
        center.client.post(
            path,
            json={"action": "resume"},
            headers=center.operator_headers(**{"Idempotency-Key": "resume-1"}),
        ).status_code
        == 403
    )
    assert (
        center.client.post(
            path,
            json={"action": "inspect"},
            headers=center.operator_headers(**{"Idempotency-Key": "inspect-1"}),
        ).status_code
        == 202
    )
