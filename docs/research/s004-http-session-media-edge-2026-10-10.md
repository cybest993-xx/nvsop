# S004：默认 HTTP 会话、授权媒体及主机签名实测

本报告记录 [#192/S004](https://github.com/cybest993-xx/nvsop/issues/192) 在 2026-10-10 按用户最新决定改用**默认 HTTP** 所执行的验收。真实测试通过 Docker Compose、Tilt、Nginx、Center API/Worker、PostgreSQL、Redis、Chromium，以及按照产品签名协议生成的合成 Edge 主机请求；**不是 HTTPS 证书链验收、生产部署、真实边缘硬件或 #193/#194 完整跨模块业务验收**。

软件行为权威仍为[开发环境操作手册](../deployment/development.md)、[推理机自治机制](../design/mechanisms/edge-autonomy.md)与当前主线；本文仅是日期化验收证据，不是第二套现行规范。

## 身份和边界

- 官方已接受基线：`origin/main@219d1c9388b55496925f31eab2f60e5559644cc7`（#191/PR #450 合并）。本机隔离的 `.nvsop/artifacts/validation/s004-20261010/fixture-source` 是从此 SHA 得到的测试 `main`，仅把 `scripts/dev.py`、`Tiltfile`、`deploy/dev/compose.yaml` 的 Docker 项目名/镜像/卷前缀从 `nvsop-dev-main` 替换为 `nvsop-s004-iso`；其 fixture SHA `3ac48f7746e8ddfae9390edaa3ea687253c641d5` **不等于**正式 main 提交。应用 API、Nginx 路由与验证逻辑和接受基线完全一致。
- 本机真实入口是 `http://localhost:8443`（业务、Cookie 会话）和 `http://localhost:8444`（标注派生媒体）；`dev-setup` 不设置 HTTPS/cert/key，真实 `setup.json` 记载 `protocol=http`、`ca_certificate=null`，没有创建 CA 私钥或服务器证书。
- 原始测试凭据、签名私钥、Cookie、标注上下文 Token、样例素材、Playwright trace 及 JSON 报告只在被 Git 忽略的本机 `.nvsop/artifacts/validation/s004-20261010/{fixture-state,evidence}` 下，**不纳入 PR**；提交的只是脱敏结果摘要。测试结束后调用正式 `dev.py down`，全部隔离容器停止，四个 `nvsop-s004-iso-` named volumes 保留（`volumes_removed=false`）；共享的 `nvsop-dev-main` 数据卷未用于实验。早先 #193/S118 的隔离实例及 Job 被正常暂停、工作树及卷完整保留，不将其他 Issue 的事实算进本票。

## AC 实测

| AC | 方法及实际结果 | 结论 |
| --- | --- | --- |
| AC1 默认 HTTP 与会话 | 真实完整服务达到 `ready`、HTTP liveness 200，`dev.py smoke` 绑定上述 fixture SHA、`protocol=http`、`passed`、exit 0。真实 Chromium 通过 Nginx 登录 `dev.admin`，页面刷新后 `GET /api/v1/auth/session` 200；session Cookie `HttpOnly=true`、`SameSite=Strict`，HTTP 开发态 `Secure=false`，CSRF Cookie 可供前端读取且为 Strict；不带 CSRF 的登出为 403，带正确 CSRF 登出为 204，后续读取已撤销会话为 401。 | 通过 |
| AC1 管理鉴权 | 第二条真实 Chromium 浏览器测试：未带 CSRF 的已登录创建主机请求为 403，正确 CSRF 后 `POST /api/v1/inference-hosts` 为 201，随后使用 `If-Match: 1` 和 CSRF 登记合成主机**公钥**为 200（修订 2）。未注册任何生产 Edge；私钥只在忽略的测试目录。 | 通过 |
| AC2 Edge 七条主机签名路由 | 经过真实 HTTP Nginx，使用浏览器先登记的主机公钥及合成 Edge 私钥按正式 `nvsop_contracts.HostIdentityRequest` 签名。对配置 GET、历史配置确认 POST、监控判定 POST、实例 POST、观测 POST、健康 POST、待命令 GET 共 **7 条**路由实测：有效签名依次得 `200,422,409,409,409,409,204`，伪造签名七条全部得 `401`。其中 confirmed-configuration 的 `422` 是测试所传空 bundle 未通过共享配置 wire 解析器校验；监控接口的 `409` 是身份认证之后业务用例拒绝，均非业务操作成功。相同签名 nonce 重放返回 `401`；签名时间过期返回 `401`；对 `/device-commands/next` 的无头请求返回 `422`。 | 通过：网关与签名认证边界；非七条业务成功 |
| AC2 授权媒体 Range | 第三条真实 Chromium 测试登录后，经已有 `POST /api/v1/training-datasets/{dataset_id}/members/{member_id}/annotation-context` 让**真实 Worker** 生成标注上下文，API 报准备 `succeeded`；使用该上下文的原始视频媒体 URL 经过真正的 `localhost:8444` Nginx 发 `Range: bytes=0-63`，得到 `206 Partial Content`、`Content-Range: bytes 0-63/…`、实际 64 字节；无 Session 的访问被 `401/403` 拒绝，错误上下文被 `401/403/404` 拒绝。最初测试 URL 使用 `127.0.0.1:8443` 而媒体配置为 `localhost:8444`，由于 Cookie 作用域不同得到 401；使用正式文档的 `localhost:8443` 入口复验后 **通过**。同源身份的这一细节需保留，不将首次失败伪称成功。 | 通过 |
| AC3 安全与文档 | HTTP 下没有绕过 CSRF、Nginx 媒体授权、主机签名或 nonce 防重放。三条成功 Chromium 测试各自有真实 JSON 报告，登录和媒体测试产生截图；本地 trace ZIP 不计为成功测试证据。原始证据留在本地，临时的三条 fixture-only Playwright spec 已移除，测试副本 Git 树干净。开发说明明确本地 Center/Nginx 默认 HTTP，Edge 机制文档明确推理机运行时 `center_url` 仍只允许 HTTPS；HTTPS 证书链、浏览器 TLS 校验**本票未验证**。 | 通过 |

三条 Playwright fixture 浏览器用例在 `chromium-1366x768` 上分别为 **1 passed、0 skipped、0 failed**。真实主机签名结果摘要位于忽略的 `evidence/signed-machine-http-gateway.json`，三个 JSON 报告按被测 SHA 命名，登录及媒体截图在 `evidence/browser/{SHA}`、`evidence/media-range/{SHA}`；其中的 trace ZIP 属于失败/重跑诊断，不计为成功用例证据。三条测试的 JSON 分别为 `browser-{SHA}.json`、`host-registration-{SHA}.json`、`media-range-{SHA}.json`（均在 `evidence/` 下）。在隔离 fixture 的 `apps/control-web` 目录按测试文件分别使用 `pnpm exec playwright test s004-http-session-live.spec.ts --project=chromium-1366x768`、`pnpm exec playwright test s004-http-signed-host-registration.spec.ts --project=chromium-1366x768`、`pnpm exec playwright test s004-http-media-range-live.spec.ts --project=chromium-1366x768`；相关环境与登录凭据仅在本地测试状态目录，fixture-only spec 已在验收后移除。

## 验收归属和未覆盖项

本票的真实证据证明本地默认 HTTP 的会话、CSRF、授权媒体 Range 与主机身份**签名边界**可用。没有构造完整的多工位、真实设备和监控业务事实，所以对测试主机业务返回 `409/422` 的处理链不算业务功能验收通过；这些属于后续 #193/#194。生产 Edge 服务的 `center_url` 当前仅接受 HTTPS；本票仅验证合成签名请求经实际 HTTP 网关的身份边界，并未使用生产 Edge runtime 连接 HTTP；可选 HTTPS 的外部证书约定及 #451 的软件问题**独立留存，没有被本次 HTTP 验收修复、关闭或验证**。#192 的验收口径以用户本次明确修改后的 GitHub Issue AC1–AC3 为准，不将旧 HTTPS AC 记通过。
