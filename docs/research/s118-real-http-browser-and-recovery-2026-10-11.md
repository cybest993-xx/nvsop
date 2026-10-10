# S118：默认 HTTP 下配置、逐视频上传和 NVIDIA 标注跨模块真实验收

本记录对应 [#193/S118](https://github.com/cybest993-xx/nvsop/issues/193) 的 AC1–AC3。真实测试针对已接受的 `origin/main@d5bdf99fba29969437e10a86518a0522341485e3`（包含标注小片修复 [#453 / PR #454](https://github.com/cybest993-xx/nvsop/pull/454)），使用隔离 Docker/Compose、真实 PostgreSQL/Redis/Worker、Nginx、Annotation Backend、Chrome 与 Edge。测试只用合成视频，不用客户素材和 GPU/相机；默认协议是 **HTTP**，不声称已验收可选 HTTPS。

软件/接口及运维步骤的规范仍归 [开发环境操作手册](../deployment/development.md)、[标注部署证据](../deployment/annotation-evidence.md)与代码/Issue，本文件仅保留本次不可替代的**现场证据和边界**。

## 基准与隔离

- 被验收主线：`d5bdf99fba29969437e10a86518a0522341485e3`。只在 Git 忽略的 `.nvsop/artifacts/validation/s118-final-20261011/source` 测试副本中将 `scripts/dev.py`、`Tiltfile`、`deploy/dev/compose.yaml` 的项目、镜像及卷前缀 `nvsop-dev-main` 换为 `nvsop-s118final-iso`，提交为 fixture SHA `dfb2679d60468be65290d77ee554fd3546aaa7c6`；业务源代码与主线字节一致。额外 SYS-31 和浏览器测试文件仅存在于忽略的 fixture 内；不提交到产品树。
- 真 HTTP 地址：`http://localhost:8443`；由原 `scripts/dev.py setup/run/status/smoke/down` 和 Compose 提供的 Nginx、Center API/worker、PostgreSQL、Redis、NVIDIA Annotation Backend/Frontend，非 Mock 服务。保留四个 `nvsop-s118final-iso-` 命名卷（数据库、Redis、dataset media、annotation media）；没有触碰共享 `nvsop-dev-main-` 卷。
- 合成媒体是 FFmpeg H.264 MP4；账号密码、Cookie、上传对象键、标注上下文 token、原始跟踪 ZIP、视频与服务日志只保存在 Git 忽略的本机 `.nvsop/artifacts/validation/s118-final-20261011/{state,evidence}`，本报告不包含凭据/用户数据。

## 真实 AC 实测

| AC | 端到端执行与断言 | 结果 |
| --- | --- | --- |
| AC1 — 登录、授权、设备/模板/概览 | Chrome 1366×768 与 Edge 1920×1080 分别经 Nginx 实际登录、刷新恢复会话、打开概览/工位设备/模板/训练数据集；未提供 CSRF 的注销为 403，匿名会话为 401；SYS-31 接口场景包含越权数据集拒绝及旧兼容写入不绕过正式业务授权。 | **通过**，两个浏览器均有独立实际场景。 |
| AC2 — 本地视频逐个上传与处理 | 两个真实浏览器分别选择目标 dataset、选择合成视频，经 `PUT .../attempts/.../content` 得到 204，`POST .../confirm` 得到 202，等待 Worker 实际校验达到 `registered`；实际大小、SHA-256 与本地上传文件相符，编码不为空。源文件落入隔离 `dataset-media` 持久卷；SYS-31 直接 HTTP 权限/异常通路不会把越权写入当作成功。 | **通过**，未以旧 MinIO/S3/presign 假设替换当前中心流式 PUT。 |
| AC3 — 标注与派生媒体 | Chrome 与 Edge 分别真实进入 NVIDIA React 编辑器、加载授权视频并在时间轴输入有效事件，提交 `POST /api/annotation/api/v1/videos/{id}/split` 后等待异步完成并看到真实成功；SYS-31 HTTP 断言原视频 Range 206 与会话撤销后的拒绝；显式重启后的正式 smoke 另行核实原媒体与派生切片 Range 都为 206。先前的短片 500 软件缺陷已由已合并 #453 修复；不以该票修复前失败结果代替此轮成功。 | **通过**：真实两个浏览器 + SYS-31 HTTP；故障/恢复见下。 |

`pytest -q -s tests/system/test_s118_live_http.py` 在真实部署返回 **4 passed, 0 failed**（10.82s）；`pnpm exec playwright test s118-real-http-browser.spec.ts s118-real-http-annotation.spec.ts --project=chrome-1366x768 --project=edge-1920x1080 --workers=1 --timeout=240000` 返回 **6 passed, 0 failed**（18.9s），三条场景在两个浏览器分别运行。无 skip 计入通过。

## 故障注入、显式恢复与真实限制

同一隔离实例使用 `docker stop/start` 逐个注入 `worker`、`redis`、`annotation-backend`、`center-db` 不可达，每个均验证 `dev.py status` 呈 `degraded`、目标容器不运行，并在恢复健康后验证 `ready`，日志与状态按步骤保存。**Redis 中断还会导致 Worker 退出**；单纯重启 Redis 不足以使 Worker 恢复，测试明确检测到 Worker 的 `exited`，由操作者显式启动该 Worker 后再获得健康与 ready，不宣称它自动恢复。

PostgreSQL 恢复后、没有重新启动消费者的一次`dev.py smoke` **实际失败**，报“标注基座请求失败（HTTP 500）”；这说明单纯 Compose 健康/ready 并不保证所有已有连接已可立即完成标注业务。该失败保留在 `evidence/smoke-post-outages.log` 与 `evidence/s118-job-summary.json`，不能改写为成功。之后通过正式 `dev.py down`（保留数据卷）和 `dev.py run` 完整重启同一隔离实例，再次执行正式 `dev.py smoke` **exit 0 / passed**：标注准备 `succeeded`、切片执行 `succeeded`、生成一个切片、源媒体与切片 Range 均为 **206**，任务数据仍可读。记录的是**显式重启完成的业务恢复**，不是无操作自愈或没有发生过 500。

训练素材卷独立进行真实读失败验证：在确认卷的 Compose 项目标签属于 `nvsop-s118final-iso` 后，只将样例数据集已登记的**单个合成** `registered-video` 文件在同卷中临时改名，不删除、不改变字节。经过真实 HTTP 登录、Center/Worker 的新一轮标注准备产生 `failed` 与原始原因码 `OBJECT_NOT_FOUND`，没有假成功；恢复原文件并核对 SHA-256 完全相等后，新请求准备为 `succeeded`。两个动作由同一带 `finally` 恢复路径执行（Job `wc_job_5JGjGYtWC1pv92mj`），服务经正式停止入口退出、原四卷保留。这只覆盖所选合成源对象的真实暂时缺失，不冒充底层磁盘整体损坏测试。

## 机器证据、仍存边界

- 主线全量功能/浏览器与四服务故障流程：`evidence/pytest-real-sys31-http.log`、`evidence/playwright-real-chrome-edge.log`、`evidence/s118-job-summary.json` 及每步 `*-unhealthy-status.json`、`*-recovered-status.json`；第二次完整流程 Job `wc_job_5eH_Vu_kATvUPXVa` 的最终冒烟失败如上如实保留。
- 故障后的实际业务恢复：`evidence/postoutage/smoke.log`、`state/reports/smoke-dfb2679d60468be65290d77ee554fd3546aaa7c6.json`、`evidence/postoutage/{center-api,annotation-backend,worker,center-db}.log`；Job `wc_job_qh4me4iJd_RfIw3W` exit 0，官方 `down` exit 0。
- 素材卷丢失/恢复：`evidence/volume/unavailable-status.json`、`evidence/volume/restored-status.json`、`evidence/volume/{worker,center-api}.log`、`evidence/volume/down.log`；Job `wc_job_5JGjGYtWC1pv92mj` exit 0。
- 此环境是 WSL 隔离固定 main 的软件路径；没有真实边缘 GPU、相机、客户视频、生产部署、TLS 证书和在线现场标注硬件。数据库恢复后无需人工操作即完整自愈**未验证且确实观察到一次不满足**；不将其计入通过，也不扩大本 validation Issue 成新的连接池/服务编排实现。

最终判断：#193/S118 AC1–AC3 所要求的真实浏览器功能、授权流式文件、后台校验、标注、故障状态及经**显式操作**完成的恢复均有实际证据。#194/S120 的 Edge 运行/违规/证据/人工复核端到端路径属于另一个 Issue，不在本次验收范围。
