#!/usr/bin/env python3
"""通过正式 control-api API 导入训练视频的命令行薄适配器。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

# 直接从仓库执行时补上 control-api 源码路径；正式逻辑仍由 dataset 模块拥有。
_CONTROL_API_SOURCE = Path(__file__).resolve().parents[1] / "apps" / "control-api" / "src"
if str(_CONTROL_API_SOURCE) not in sys.path:
    sys.path.insert(0, str(_CONTROL_API_SOURCE))

from factory_sop.dataset.client import (  # noqa: E402
    _DEFAULT_JOB_POLL_INTERVAL_SECONDS,
    _DEFAULT_JOB_POLL_TIMEOUT_SECONDS,
    API_PREFIX,
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    ControlPlaneClient,
    DatasetImportError,
    HttpResponse,
    HttpTransport,
    UrllibTransport,
    _required_string,
    import_training_dataset,
)

__all__ = [
    "API_PREFIX",
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "ControlPlaneClient",
    "DatasetImportError",
    "HttpResponse",
    "HttpTransport",
    "UrllibTransport",
    "import_training_dataset",
    "main",
]


def _parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="通过正式控制面 API 导入训练视频")
    parser.add_argument("--base-url", required=True, help="中心后台的 HTTP(S) 地址")
    parser.add_argument("--dataset-name", required=True, help="新训练数据集名称")
    parser.add_argument("--source", required=True, help="视频来源说明")
    parser.add_argument("--session-cookie", help="已有 sop_session cookie")
    parser.add_argument("--csrf-token", help="与 sop_csrf cookie 对应的 x-csrf-token")
    parser.add_argument("--login-name", help="通过 /api/v1/auth/session 登录的账号")
    parser.add_argument("--password-file", type=Path, help="登录密码文件")
    parser.add_argument(
        "--job-poll-interval",
        type=float,
        default=_DEFAULT_JOB_POLL_INTERVAL_SECONDS,
        help="任务轮询间隔（秒）",
    )
    parser.add_argument(
        "--job-poll-timeout",
        type=float,
        default=_DEFAULT_JOB_POLL_TIMEOUT_SECONDS,
        help="任务轮询超时（秒）",
    )
    parser.add_argument("videos", nargs="+", type=Path, help="逐个上传的视频文件")
    return parser.parse_args(argv)


def _client_from_arguments(arguments: argparse.Namespace) -> ControlPlaneClient:
    client = ControlPlaneClient(
        UrllibTransport(),
        base_url=arguments.base_url,
        session_cookie=arguments.session_cookie,
        csrf_token=arguments.csrf_token,
    )
    has_login = arguments.login_name is not None or arguments.password_file is not None
    has_cookie = arguments.session_cookie is not None or arguments.csrf_token is not None
    if has_login and has_cookie:
        raise DatasetImportError("登录参数与已有 cookie 参数不能混用")
    if has_login:
        if arguments.login_name is None or arguments.password_file is None:
            raise DatasetImportError("登录必须同时提供 --login-name 和 --password-file")
        password = arguments.password_file.read_text(encoding="utf-8").rstrip("\r\n")
        client.login(login_name=arguments.login_name, password=password)
    elif not (arguments.session_cookie is not None and arguments.csrf_token is not None):
        raise DatasetImportError(
            "请提供 --login-name/--password-file，或已有 session cookie 和 CSRF token"
        )
    return client


def main(
    argv: Sequence[str],
    *,
    client: ControlPlaneClient | None = None,
) -> int:
    """命令行入口；失败时返回非零而不打印会话凭据。"""
    arguments = _parse_arguments(argv)
    try:
        control_plane = client if client is not None else _client_from_arguments(arguments)
        result = import_training_dataset(
            control_plane,
            dataset_name=arguments.dataset_name,
            source=arguments.source,
            videos=arguments.videos,
            poll_interval_seconds=arguments.job_poll_interval,
            poll_timeout_seconds=arguments.job_poll_timeout,
        )
    except (DatasetImportError, OSError, ValueError) as error:
        print(f"导入失败：{error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "dataset_id": _required_string(result.dataset, "id"),
                "videos": [
                    {
                        "filename": item.path.name,
                        "member_id": _required_string(item.member, "id"),
                        "attempt_id": _required_string(item.attempt, "id"),
                        "job_id": _required_string(item.job, "id"),
                        "job_status": _required_string(item.job, "status"),
                    }
                    for item in result.videos
                ],
            },
            ensure_ascii=False,
        )
    )
    if any(item.job.get("status") in {"pending", "enqueued", "running"} for item in result.videos):
        print("导入未完成：至少一个视频的校验任务仍在运行", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
