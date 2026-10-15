"""网关主机签名白名单必须覆盖 Edge 两个上报入口，且两份随仓模板规则一致。

按 Nginx `location ~` 的 search 语义（不是 Python fullmatch）验证，避免丢掉 `^`/`$` 锚定后
仍"看起来通过"。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIGS = (
    REPO_ROOT / "deploy/dev/nginx.conf",
    REPO_ROOT / "docs/deployment/nginx-annotation.conf.example",
)
MACHINE_LOCATION_PREFIX = 'location ~ "^/api/v1/(?:inference-hosts/'
# 独立 literal 预期：monitor 组必须同时覆盖 enveloped 判定与处置上报，其它路由不放宽。
EXPECTED_MONITOR_GROUP = (
    "monitor/(?:reported-decisions(?:/enveloped)?|reported-disposals"
    "|reported-instances|reported-observations|health)"
)


def _machine_whitelist_rule(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    line = source[source.index(MACHINE_LOCATION_PREFIX) :].splitlines()[0].strip()
    match = re.fullmatch(r'location ~ "(.+)" \{', line)
    if match is None:
        raise AssertionError(f"unexpected machine location line in {path}: {line}")
    return match.group(1)


class MachineGatewayWhitelistTest(unittest.TestCase):
    def test_both_templates_share_one_monitor_rule(self) -> None:
        rules = [_machine_whitelist_rule(config) for config in CONFIGS]
        self.assertEqual(rules[0], rules[1])
        self.assertIn(EXPECTED_MONITOR_GROUP, rules[0])

    def test_rule_opens_edge_reporting_and_keeps_browser_routes_out(self) -> None:
        rule = _machine_whitelist_rule(CONFIGS[0])
        for uri in (
            "/api/v1/monitor/reported-decisions",
            "/api/v1/monitor/reported-decisions/enveloped",
            "/api/v1/monitor/reported-disposals",
            "/api/v1/retention/reported-impact/access-check",
            # #206 的三个主机签名删除资格动作：claim / verify / result 都只经这一条白名单。
            "/api/v1/retention/evidence-deletion-claims",
            "/api/v1/retention/evidence-deletion-claims/verify",
            "/api/v1/retention/evidence-deletion-claims/result",
        ):
            self.assertIsNotNone(re.search(rule, uri), uri)
        for uri in (
            "/api/v1/monitor/decisions",
            "/api/v1/retention/policy",
            "/api/v1/retention/policy/impact-estimate",
            "/api/v1/retention/reported-impact/access-check/extra",
            "/api/v1/monitor/violations",
            "/api/v1/monitor/reported-decisions/arbitrary-extra",
            "/api/v1/monitor/reported-decisions/enveloped/extra",
            "/api/v1/monitor/reported-disposals/extra",
            # 浏览器会用的证据/保留路由不能被这条机器白名单放宽。
            "/api/v1/retention/evidence-deletion-claims/arbitrary-extra",
            "/api/v1/retention/evidence-deletion-claims/verify/extra",
        ):
            self.assertIsNone(re.search(rule, uri), uri)


if __name__ == "__main__":
    unittest.main()
