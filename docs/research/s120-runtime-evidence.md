# S120 跨模块运行/告警/证据验证记录

Status: **dated evidence**（记录受控环境实测，不定义产品要求，也不替代需求）。归属 Issue #194（S120，live 状态见 GitHub）。

- 本轮实测日期：2026-10-12。
- 本轮产品基线：`main@622ef89de5b782a00d7bc2b7fcd172135803baf5`（运行前从已接受 main 无冲突快进，保留全部 WIP）。
- 本轮代码提交：`38c903e49cfdb8246bea726f9ae74af38a6bdc04`（source commit；本记录随后单独提交，source 未再改动）。
- 历史产品基线：`main@079af6854deaffd4fad67dcb06f43e8f8fddaff2`（base79，CSP 阻塞轮）与 `main@d67e9fa0ef37678f1a11e8f0b23372218d97ce5d`；其结论只作历史，不与本轮混用。

## 交付刷新（2026-10-13，product base `main@1561ff3…`）

对已接受 main 的无冲突快进已在交付前完成，本段是同一 candidate 的交付刷新记录。

- 刷新日期：2026-10-13（`date -u` 确认）。
- 产品基线：`main@1561ff3e2dd1d2f822e2be88455dd55f6860a14e`（[PR #490](https://github.com/cybest993-xx/nvsop/pull/490)，仅 dataset 变更）。
- merge candidate：`a3190147061635ef636b719520ed4aaf6244169b`（原生 `git merge --no-edit --no-overwrite-ignore origin/main`，`merge-tree` 预检退出 0，无冲突；保留原 `38c903e`/`6d2510b` 提交，未改写历史）。
- source hash 不变：py `7114657bbca118ab5af2d29e813748284be4ea23d2e3df777de9e35b9b9e3318`、spec `7a218a50db7ab489494dcde2a8fda67a9d33a0ae188ff806124ec56935af686c`、fixture `78b067bc9f90322ee15c5b44f5e799d6ba23f1c6f5635603341aae7aa9ad27ae`。
- 新基线验证：`NVSOP_EVIDENCE_BROWSER=1 PYTHONPATH=apps/control-api/src:apps/edge-runtime/src:packages/contracts/src .nvsop/venv/bin/pytest tests/system/test_s120_runtime_evidence.py -v -p no:cacheprovider` exit 0，`3 passed in 80.77s`（AC1/AC2/AC3 各 PASSED；AC3 内 live+offline 浏览器各 `1 passed`）。raw：`.nvsop/artifacts/s120/refresh-1561ff3-1vuokwpc/pytest-s120.log`、`playwright.live.stdout`、`playwright.offline.stdout`。
- 复用理由（准确）：新 main 仅改 `apps/control-api/src/factory_sop/dataset/adapters/storage.py`、`api.py`、`usecases/usage.py` 与两个 dataset 单测，与 S120 共享网关夹具/monitor/evidence/edge/ui/deploy/contracts/locks/CI 及公开 app composition 无关，source/config/deps 未变；故复用 622 基线的完整 `make check` exit 0、12 个共享夹具回归 pass 及既有独立评审。本轮**未**重跑完整 `make check`/fixture 12，也未 `make sync`；`make web-build` 产物（`.nvsop/artifacts/web/dist`）按 source/config/deps 未变复用。

## 目的与范围

验证推理机本地证据→主机签名上报→中心 monitor 镜像/SSE→授权读取→人工复核的跨模块归属。
CPU 合成观测与 ffmpeg 合成媒体只证明**软件路径**；真实现场（GPU、真实相机、真实模型推理、真实滚动
录像切片）不在本记录，由[目标环境验证矩阵](target-environment-validation-matrix.md)的门禁负责。中心
只做镜像，不做二次判定，不持有证据媒体字节。

## 环境身份

- 产品基线：本轮为 `main@622ef89de5b782a00d7bc2b7fcd172135803baf5`；历史记录为 `main@079af685…`（base79）与 `main@d67e9fa0…`。
- 推理机本地状态：真实 SQLite（`edge_runtime.local_state` 当前 schema）。
- 中心：真实 Uvicorn + PostgreSQL（`timescale/timescaledb:2.22.1-pg17`，见 `tests/system/conftest.py::POSTGRES_IMAGE`）+ Alembic head。
- 入口：仓库真实 `deploy/dev/nginx.conf` **经公开渲染入口 `scripts/dev.py::render_gateway_config`** 渲染的 Nginx `1.27.3-alpine` HTTPS 网关。夹具只提供测试地址/端口与显式 TLS 材料，来源映射经进程环境 `SOP_EVIDENCE_HOST_ORIGINS` 进入 CSP；测试不手改 `connect-src`/`media-src`。
- 网关 TLS（仅测试）：loopback 自签证书，SAN 覆盖 `IP:127.0.0.1,DNS:localhost`，作为其自身信任锚通过公开渲染入口的 `openssl verify -verify_hostname localhost` 校验；这是**合成测试身份**，不等于现场证书。#471 的真实外部 TLS 仍在独立范围。signedEdge→center 与 source→center 的生产客户端仍按 CA 校验（`ssl.create_default_context(cafile=…)`），未关闭产品 TLS、未改全局 CA/browser store。
- 推理后端身份端点：CPU 合成的 `/v1/metadata`、`/v1/models`（外部硬件边界的软件替身，非真实模型推理）。
- 证据媒体：`ffmpeg` 合成 H.264 片段与 JPEG 关键帧（1s 窗口，与证据跨度一致）。
- 浏览器（仅 opt-in）：真实 Nginx HTTPS `location /` 反代到仅提供 `make web-build` 构建产物的 `vite preview` 静态服务（`.nvsop/artifacts/web/dist`）；API/SSE 仍由浏览器直接经 Nginx 到 Center、不经 Vite API 代理；来源媒体仍由浏览器真实直连 Edge HTTPS；Playwright chromium。

## 被测候选

本轮 source 提交 `38c903e49cfdb8246bea726f9ae74af38a6bdc04`，其内容哈希：

- `tests/system/test_s120_runtime_evidence.py` sha256 `7114657bbca118ab5af2d29e813748284be4ea23d2e3df777de9e35b9b9e3318`
- `apps/control-web/tests/e2e/s120-runtime-evidence.spec.ts` sha256 `7a218a50db7ab489494dcde2a8fda67a9d33a0ae188ff806124ec56935af686c`
- `tests/system/test_sys_machine_identity_over_nginx_https.py` sha256 `78b067bc9f90322ee15c5b44f5e799d6ba23f1c6f5635603341aae7aa9ad27ae`（共享网关夹具新增公开渲染接缝）
- `docs/README.md` sha256 `43f55cafa51b2425cf1324bd249b7354692d6ee74314ddd14100d76c0812f51b`
- 本文件（`docs/research/s120-runtime-evidence.md`）

历史候选（不与本轮混用）：

- base79 阻塞轮：py sha256 `050fe73729a186d8e4726d4345893971f0d8fc87edcd6c5ef524fc476326b74d`、spec `7a218a50…`。
- base `main@d67e9fa0…`：`make check` exit 0 的 py sha256 `11601301fde7db45fb69b4cc2ee300ccdb55262098fa0f4a5b5f8c824840da76`；浏览器 live+offline 各 `1 passed` 的 py sha256 `8a92bb78ba89b34fde2e70d674492fafa9a69d7d64d15ddc6d47d12fd51bbf5a`、spec `0c5ba39d…`。

这些是任务 worktree 候选哈希，不是发布或发布内容事实。

## 命令与结果（实际执行）

工作目录为任务 worktree，Python 用本任务 `.nvsop/venv`（真实 worktree 自有 venv，`.pth` 指向本 worktree，非 shared symlink）。

### 本轮（2026-10-12，base `main@622ef89…`，source commit `38c903e`）

| 命令 | 结果 | 原始日志 |
|---|---|---|
| `make sync` | exit 0 | 终端 |
| `make web-build` | exit 0；产物 `.nvsop/artifacts/web/dist` | 终端 |
| `NVSOP_EVIDENCE_BROWSER=1 PYTHONPATH=apps/control-api/src:apps/edge-runtime/src:packages/contracts/src .nvsop/venv/bin/pytest tests/system/test_s120_runtime_evidence.py -v -p no:cacheprovider` | exit 0，`3 passed in 96.99s`（AC1/AC2/AC3 各 PASSED；AC3 内 live+offline 浏览器各 `1 passed`） | `.nvsop/artifacts/s120/run-20261012T225050Z/pytest-s120.log`、`.nvsop/artifacts/s120/browser/playwright.live.stdout`、`.nvsop/artifacts/s120/browser/playwright.offline.stdout` |
| 共享网关夹具受影响回归：机器身份 http+https、monitor SSE 5、CSP 3、sys35 非浏览器 2 | exit 0，`12 passed in 117.01s` | `.nvsop/artifacts/s120/run-20261012T225255Z-fixture/fixture-regression.log` |
| `CHECK_JOBS=1 make check` | exit 0 | `.nvsop/artifacts/s120/run-20261012T225050Z/make-check.log` |

本轮 opt-in 浏览器 live 与 offline 阶段均真实执行并通过；测试自己拥有的 gateway 故障注入（`docker exec` 精确容器 + 仅向 PID1 nginx master 的 worker 子进程 SIGTERM，不 signal PID1 master）触发真实 EOF 后由原生 `EventSource` 续接。

### 历史（base79 与 d67，不与本轮混用）

| 命令 | 结果 | 原始日志 |
|---|---|---|
| base79：`NVSOP_EVIDENCE_BROWSER=1 pytest tests/system/test_s120_runtime_evidence.py::test_s120_ac3_… -v -p no:cacheprovider` | exit 1，`1 failed`；来源授权/读取 Python 段通过，浏览器 UI 被 CSP `connect-src` 阻断（见历史阻塞） | `.nvsop/artifacts/s120/production-origin-20261012T201236Z/ac3-optin.log` |
| base d67：`pytest tests/system/test_s120_runtime_evidence.py -q -p no:cacheprovider` | exit 1，`1 failed, 2 passed`；AC3 网关 SSE 响应头阶段超时（见历史发现） | `.nvsop/artifacts/s120/s120-all-python-final.log` |
| base d67：`CHECK_JOBS=1 make check` | exit 0 | `.nvsop/artifacts/s120/make-check-final-b.log` |

## 验收矩阵（actual，本轮 source commit `38c903e`）

| AC | 状态 | 实际证据 |
|---|---|---|
| AC1 同一实例/观测/健康/判定/锁存违规/处置/证据引用可追溯，中心只镜像不重判、不存媒体字节 | **本轮通过** | Edge 真实三步模板 + `LocalState` + `StationSupervisor`（step0→step2 跨步得 `WRONG_STEP`+`MISSED_STEP` 锁存，后空闲结案）→ `HttpDecisionReportTransport`/`EvidenceReferenceReconciler` → 网关；中心公开 API 核对 violations（同 `decision_event_id`、`latched_at`、host/station/instance）、instances（`template_version_id`/`sha256`+backend `model_ids` 回溯）、observations、stream-health、disposals（`record`/`frontend_alert`）、evidence 引用（`clip`+`keyframe`，sha256/size/reference；媒体字节仅在本机来源目录） |
| AC2 重复上报/重复登记幂等、SSE 断线续接不丢不重、模板切换/历史积压、未知原因保留原码 | **本轮通过** | 网络边界丢一次 enveloped 响应→同 `event_id` 重发命中中心幂等；真实 v1 上报后经公开 bind 换绑 v2、Edge 拉取确认 v2，旧 v1 队列仍在本地并重发，SSE 帧保留 v1 `template_version_id`/`sha256`/`configuration_revision`/backend provenance；未知 `FUTURE_REASON` 原码保留且来源不变；证据同稳定 ID 重复登记不产生第二份；Last-Event-ID 续接只送游标之后；浏览器真实 nginx worker TERM 断连→error→原生续接后同来源归档数不变、0 新增提示 |
| AC3 授权读取来源主机媒体后复核不改原判定；越权/失效会话/离线/媒体缺失/部分失败可见 | **本轮通过** | 真实 HTTPS 来源服务；按票据 206 Range 读取并解码；已登录但缺 `evidence` 权限的用户 403；媒体缺失 404 `media_missing`；登出后 403 `session_invalid`；部分失败（一份媒体真实缺失、另一份正常且中心引用仍在）；复核另存后原 violations 逐字不变；浏览器 live 阶段从非 8444 来源经渲染网关 CSP 解码并 Range 读取、offline 阶段真实停机显示不可达 |

## 本轮修复接缝（source commit `38c903e`）

base79 记录的浏览器 CSP 首阻断来自 #487 部署缺陷（`SOP_EVIDENCE_HOST_ORIGINS` 未进入网关 CSP）。该缺陷已由 #487/[PR #489](https://github.com/cybest993-xx/nvsop/pull/489) 在 `scripts/dev.py`（`render_gateway_config` 经 `inject_csp_evidence_origins` 向 CSP 追加来源 origin）与 `deploy/dev/compose.yaml`（中心环境同源映射）修复，并配套 `docs/deployment/configuration.md` 与 `tests/system/test_sys_evidence_origin_csp_over_nginx.py`；`deploy/dev/nginx.conf` 模板本身未改变。修复随本轮 base `main@622ef89…` 纳入。

本轮只做最小接线：共享网关夹具 `tests/system/test_sys_machine_identity_over_nginx_https.py` 改为调用公开渲染入口 `render_gateway_config`（测试只注入地址/端口与显式 TLS 材料），AC3 用 `monkeypatch` 把同一 `SOP_EVIDENCE_HOST_ORIGINS` JSON 交给该入口。测试不手改 CSP、不为迎合 `localhost:8444` 改端口、不调用低层 inject 替代公开配置证明。夹具证书改为覆盖 `127.0.0.1` 与 `localhost` 的 loopback 自签证书，以继续通过公开渲染入口的显式 TLS 校验。

## 历史阻塞发现（base79，已由 #487/#489 修复，保留原样）

> 状态：`connect-src`/`media-src` 未随部署来源映射放行的问题已修复并纳入本轮 base。以下为修复前历史记录，不代表当前状态。

- 来源授权/读取本身通过：中心 `/evidence/.../access-context` 返回 200，Python 侧来源 Range 读取返回 206。
- 浏览器被 CSP 阻断：`connect-src 'self' https://localhost:8444` 拒绝连接配置来源 origin（本记录不包含真实 context/cookie/签名；原始 trace 仅存本地 ignored，不公开）。当前 UI 媒体 decode / UI 复核当时未验证。
- 产品归类（confirmed）：[证据访问授权](../deployment/configuration.md#证据访问授权)允许登记任意合规 HTTPS origin，但网关模板的 CSP 未由渲染追加该来源；未改部署/策略（超出验证票授权），由 #487 独立修复。

## 历史发现（base `main@d67e9fa0…`，已由 #485/#486 修复，保留原样）

> 状态：monitor SSE 响应头阶段超时经 Issue #485 / [PR #486](https://github.com/cybest993-xx/nvsop/pull/486) 修复，并随 base79 纳入。以下为修复前历史记录。

- 同一中心、同一会话、同一路径 `/api/v1/monitor/stream`：直连中心 HTTPS 首帧到达；经真实 Nginx 网关 `client.stream(...).__enter__` 在 8s 内未取得响应头并抛 `httpx2.ReadTimeout`（`httpcore2/_sync/http11.py:167 _receive_response_headers`）。
- 复现命令：`pytest tests/system/test_s120_runtime_evidence.py::test_s120_ac3_… -q`；复现日志 `.nvsop/artifacts/s120/ac3-sse-compare.log`、`.nvsop/artifacts/s120/ac3-sse-probe.log`。

## 未验证项与边界（非 pass）

- 真实现场（GPU/真实相机/真实模型/真实滚动录像切片）由目标环境门禁负责；CPU 软件证据不覆盖，本记录不标称通过。
- 浏览器 TLS 为 loopback 合成测试身份，不冒充 #471 的真实外部 TLS；signedEdge→center 与 source→center 生产客户端仍按 CA 校验。
- 本轮只改动验证候选与网关夹具接缝；产品源码、部署、CI、contracts、lockfiles、其他 system/集成测试均未改动。

## 未改动

生产源码、部署、CI、contracts、lockfiles、其他 system/集成测试未改动。原网关前置探测源码与红日志保存在
`.nvsop/artifacts/s120/original-gateway-probe.py.txt` 与 `.nvsop/artifacts/s120/s120_probe.log`。真实浏览器 trace 始终为本地 ignored、不公开；本记录只保留脱敏事实。

本文件是 dated 实测记录，不是长期复制的任务计划；GitHub #194 的 live 状态以其自身为准。
