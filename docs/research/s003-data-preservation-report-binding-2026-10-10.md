# S003 固定开发实例数据保留与版本化报告：本地实测

此文是 [#191/S003](https://github.com/cybest993-xx/nvsop/issues/191) 的日期化验收证据，2026-10-10 在本地 WSL/Linux 真实 Docker Compose、Tilt、PostgreSQL/TimescaleDB、Redis、Center API、Worker、Nginx 和 Chromium 上测得；软件行为权威仍为 [开发环境操作手册](../deployment/development.md)及当前主线，本文**不是**第二套现行规范，也不代表生产/硬件环境或 #193/#194 的跨模块业务验收。

## 基线、隔离及证据位置

- 已接受 `main@b772a667ce4e7b5f3fc8ee07b2fb06b61b89f05f`（#190/S002 验收记录通过 [PR #449](https://github.com/cybest993-xx/nvsop/pull/449) Squash 合并所得；其底层迁移保护修复在 [PR #448](https://github.com/cybest993-xx/nvsop/pull/448)）。在忽略目录 `.nvsop/artifacts/validation/s003-20261010/` 从该已接受提交创建独立 `main` fixture，**仅**将 `scripts/dev.py`、`Tiltfile`、`deploy/dev/compose.yaml` 内 Docker 项目名、镜像/卷名前缀 `nvsop-dev-main` 改为 `nvsop-s003-iso`；其初始 fixture SHA 是 `2b4fcba21b77a5de3017cb7b513aa6e8699ffd7d`，第二次提交 `43d3a67726e096019103bcaa4d44952c49189cf0` 只添加标记文件。fixture SHA **不等于**正式 main SHA，也未修改生产运行逻辑。
- 两个真实固定版本均使用 `http://localhost:8443`。只触碰隔离的四个 named volumes：`nvsop-s003-iso-center-db`、`nvsop-s003-iso-redis`、`nvsop-s003-iso-dataset-media`、`nvsop-s003-iso-annotation-media`；共享 `nvsop-dev-main` 数据卷未参与故障注入。原始机器证据（JSON、日志、截图/trace、数据库比对）仅在上述忽略目录的 `state/` 和 `evidence/` 下，**不提交报告原文、凭据或测试媒体**。

## 验收结果

[#191 的正式 AC](https://github.com/cybest993-xx/nvsop/issues/191)：AC1 重复启动、停止、刷新保留账号及人工修改，不重复灌入样例，dev-down/Ctrl+C 不删除卷或停止其他 Compose 项目；AC2 报告与截图/trace 绑定被测 SHA、主线更新后旧通过结果明确过期；AC3 服务/Worker 故障、恢复、测试失败如实可见，报告可访问且无密码、私钥或客户媒体。以下逐条对应。

| AC | 真实执行与可观测结果 | 结论 |
| --- | --- | --- |
| AC1 账号与人工修改 | 在独立 PostgreSQL 对合成 `dev.admin` 账号设置人工显示名称，记录账号密码哈希的摘要及样例实体 ID/数量（不提交密码或哈希值）；执行重复真实 `dev_seed.py`、停止再启动、主线提交更新以及服务故障恢复后，人工显示名称、密码哈希摘要、账号/模板/数据集/成员/动作列表数量（各 1）及主键始终一致。真实登录和样例功能冒烟成功。 | 通过 |
| AC1 停止范围 | `dev.py down` 真实返回 `{"stopped": true, "volumes_removed": false}`，四个隔离卷保留，固定端口释放；另起独立 Compose Redis 项目，停止 S003 后该项目仍运行并回复 `PONG`。另外再次启动唯一真实 S003 launcher，并对核对进程身份后的该进程发送 `SIGINT`（Ctrl+C 同路径），launcher 正常退出码 0，S003 容器归零、状态变 `stopped`、四个卷和端口释放，同一 Compose 邻居仍 `PONG`；之后只清理临时邻居容器。 | 通过 |
| AC2 测试版本归属 | SHA `2b4fcba2` 的功能冒烟 `passed`、退出码 0 和 `smoke-<SHA>.json` 实际存在；提交 `43d3a677` 并自动更新就绪后，原记录在 `state.json` 明确 `stale`，仍指向**旧** `tested_sha`，没有冒充新版本验证；对新 SHA 再执行 `dev.py smoke` 后获得新报告、`passed`、退出码 0。旧文件独立保留、路径可访问。 | 通过 |
| AC2 截图/trace | 仅在 fixture 中临时创建 `apps/control-web/tests/e2e/s003-outage-evidence.spec.ts`，用 `pnpm exec playwright test s003-outage-evidence.spec.ts --project=chromium-1366x768 --workers=1` 访问**实际停止的** S003 网关（不是 mock 请求）。浏览器因连接断开真实失败（退出码 1，Playwright JSON `unexpected=1`）；该 fixture 用例在 `catch` 中显式调用 `page.screenshot({path: testInfo.outputPath(...)})` 生成 PNG，仓库现有 Playwright `trace: retain-on-failure` 生成 `trace.zip`。二者均在 `state/reports/ui/43d3a677…/`，JSON 在 `state/reports/ui-43d3a677….json`；`NVSOP_TARGET_SHA` 和测试名包含完整固定 SHA。临时 spec 随后被删除。这**不是** `dev.py test-ui` UI 模式执行，更**不计为 UI 通过**。 | 通过 |
| AC3 Worker / 服务故障 | 仅停止隔离 Worker，`dev.py status` 真实退出 1、只读显示 `degraded` 及 `worker.state=exited`，恢复后重新 `ready`/`healthy`。只停止隔离网关时，`status` 退出 1、`degraded`、HTTP liveness 不可用；恢复后状态恢复 `ready`。 | 通过 |
| AC3 测试失败与恢复 | 网关断连时真实执行 `dev.py smoke`，退出码 1、`last_smoke.status=failed`，固定 SHA `43d3a67726e096019103bcaa4d44952c49189cf0` 的 `smoke-<SHA>.json` 当时写出 `status=failed`，并在恢复前另存为本机忽略目录 `evidence/smoke-failed-report.json`。网关恢复后失败状态仍可见；主动重跑同一 SHA 的 smoke 才重新 `passed`，其成功报告会**覆盖**原本的 `smoke-<SHA>.json`（这是单路径设计，不是两份同时存在），失败记录由单独保存的副本及 `evidence/smoke-failure-state.json` 保留。 | 通过 |
| AC3 报告访问与内容 | 原始旧/新 SHA 的 smoke 文件、**另存**的同 SHA 失败 smoke 副本、Playwright JSON、PNG、trace ZIP 均在本机相应 SHA/`evidence/` 路径可访问；扫描未发现该隔离环境的 bootstrap/runtime/数据库**明文密码**或 PEM 私钥，视频仅使用生成的合成素材而非客户媒体。功能冒烟 JSON 含合成标注上下文令牌，因此原始报告只留在忽略的本机目录，不向 PR 复制。 | 通过 |

## 约束和核验边界

开发实例重启基于已被 `dev.py down` 停止的单一隔离 namespace；没有并行第二个 launcher。fixture `main` 的两次提交分别仅为容器/卷名隔离及非业务标记文件，受验收的正式 `main` 和 S003 任务工作树不受影响。真实 Ctrl+C（`SIGINT`）与 `dev-down` 分别独立执行并保留卷，之后全部测试容器关闭、临时邻居项目被清理。实际网关故障和专门生成的失败浏览器测试均保留为失败证据，**不**与成功的完整 Web E2E 混同。

本票仅覆盖 S003 软件固定实例的数据、报告与故障状态。HTTPS 真实证书及链路属于 #192，跨模块浏览器工作流属于 #193/#194，均不能由本报告替代。原始操作和路径说明见 [#191 的实测评论](https://github.com/cybest993-xx/nvsop/issues/191#issuecomment-6095611843)，以 Issue、本次 PR、独立审查和最终 CI 为交付状态的唯一来源。
