# 升级与兼容性

本文给出 NVSOP 当前可以从代码和 ADR 得出的升级规则。它不是尚未定义的备份/灾备策略替代品；[`solution-and-roadmap.md`](../design/solution-and-roadmap.md) 的 Q36 仍明确保留备份对象、周期、恢复演练和边缘 SQLite 重建/对账等未决项。

## 升级前原则

1. 从 `main` 创建独立任务分支/worktree；不要直接修改主线。
2. 先识别升级表面：中心 API/OpenAPI、数据库迁移、Web 生成客户端、边缘运行时协议、NVIDIA 基座/补丁、部署镜像/配置和持久化状态。
3. 兼容性结论由实际调用方、迁移和目标环境证据决定，不由版本号本身决定。
4. 任何依赖、运行时或基座升级都要与对应锁文件/生成输出/验证一起落地。

## 中心 API 与 Web

`/api/v1` 是固定路径前缀，不是版本轴。正常演进只能保持旧客户端兼容，例如新增可选字段或端点。

以下变化属于兼容性门禁必须拦截的破坏性变化：

- 删除路径、字段或响应能力；
- 新增必填字段；
- 收窄已有类型/取值；
- 改变既有字段或枚举值的语义。

响应枚举可以增加值，但客户端必须能把未知值降级为原始码 + 通用提示，不能空白、丢弃记录或伪装成另一个已知状态。

生成流程：

```sh
make contracts
```

该命令从 FastAPI 导出 `packages/contracts/openapi.json`，相对 `OPENAPI_BASE_REF`（默认 `origin/main`）做兼容性检查，并重新生成 Web 客户端。生成结果与源变更一起提交。

真正需要破坏性契约变化时，走“所有受影响客户端协同发布”的显式变更，而不是增加 `/api/v2` 并长期双轨维护。

## 数据库迁移

中心启动的开发 Compose 在应用前执行 Alembic `upgrade head`。仓库门禁检查迁移所有权；模块只能迁移自己拥有的表。

升级变更应至少证明：

- 从受支持的前一状态迁移到新状态成功；
- 迁移后的应用可启动并完成受影响业务路径；
- 若任务声明支持回滚，则回滚/重放语义有实际演练证据，而不是只因为 Alembic 文件存在就宣称可回滚。

**当前仓库没有完成 Q36 备份/恢复策略。** 因此不要在本文声称中心数据库、中心训练素材卷、边缘 SQLite 或推理机证据媒体已有统一备份周期、自动灾备或经过演练的恢复目标；具体交付前必须完成适用的策略和验证。

## 中心与边缘运行时

边缘运行时/可下载部署包可能与中心非同步升级，因此架构要求它们最终不能依赖 URL 版本隔离，而应通过明确的兼容契约和启动/连接时版本握手管理差异。ADR-0003 规定的目标语义是：

- 兼容组合正常接入；
- 不兼容组合**拒绝接入并显式告警**；
- 不允许“看起来还能跑”的静默降级；
- 中心离线期间继续使用最后已确认的可用配置，恢复后重新拉取并按确认语义切换。

配置同步采用[单一当前机器契约](../design/mechanisms/machine-contract-evolution.md)：Edge 使用签名主机身份从 `/api/v1/inference-hosts/{host_id}/configuration` 拉取 host-scoped bundle，共享解析器只接受当前严格格式；`config_revision` 负责单调 assignment 次序，`effective_sha256` 负责运行语义身份，`generated_at`/`producer` 只是信封元数据。兼容的非行为元数据可按已知可选字段扩展；行为字段必须绑定 `required_capabilities`，Edge 对未知能力显式拒绝。常规演进不新增 v3/v4 并行分支。

候选拉取和纯运行配置解析不会前移 durable confirmed，也不会读取或写入正在运行工位的 supervisor 状态。通过纯解析后先停止并 join 旧循环，再基于最新 SQLite 状态恢复 supervisor、构造候选 composition、切换实际 runtime，最后才原子确认同一候选；停机后的 composition 失败会重新构造旧活动配置。本地确认失败不会让新循环继续运行。历史 SQLite 中已确认的配置 v1/v2 只由 Edge local-state owner 迁移读取，共享 HTTP parser 不再接受旧 generation；成功确认会写回当前格式。

配置束的 `contract_version` 只保护**配置束 wire shape**，不能替代 ADR-0003 要求的跨全部 Center↔Edge 机器接口兼容协商。历史判定上报已单独落地一个必要的能力握手：严格 v1 继续保持原 wire shape；需要历史配置证明和多 backend provenance 的判定使用严格 v2。新 Edge 在发送 v2 前，先把该 outbox 在事件时冻结的完整 confirmed configuration 通过主机签名端点 `/api/v1/inference-hosts/{host_id}/confirmed-configuration` 提交给 Center；只有 Center 验证这是自己实际签发过的 `(host, revision, effective digest)` 并明确返回支持 decision report contract v2 后，Edge 才发送 v2 判定。旧 Center 不存在该端点时，新 Edge 保留 outbox 并报告不兼容，不删除 historical proof、也不降级成 v1。

0034 升级只把 0033 已持久化在 `device_inference_host` 的最后一次 `configuration_revision + configuration_sha256` 回填为不可变 issued-configuration 事实，**不从升级时的当前拓扑猜旧 assignment**。若升级后的 Edge 仍基于升级前已确认的 bundle 产生判定，它随 outbox 冻结该完整 bundle；之后即使工位改绑或停用，Center 仍可先按 issued digest 验真旧 bundle，再从该 bundle 固化历史 assignment 并精确校验上报。无法证明为 Center 曾签发内容的 bundle 一律不能建立历史归属。

历史判定 v2 的 backend 语义也是复数 provenance，而不是“工位配置中的第一个 backend”：Edge 在输入进入 supervisor 时保留 backend/model 来源，按 SOP instance 累积并持久化，decision 入 outbox 时冻结实际参与该实例的 backend 集合。计时器结案、重启恢复和重配置沿用该实例已保存的 provenance，不用当前配置补写历史事实。

历史判定兼容矩阵为：旧 Edge→旧 Center 继续严格 v1；旧 Edge→新 Center 继续严格 v1 和既有当前拓扑授权；新 Edge→新 Center 对 confirmed 历史判定使用握手后的严格 v2；新 Edge→旧 Center 在 v2 握手处显式失败并保留待上报队列。首次从未确认过 Center bundle 的 bootstrap 判定仍只能表达既有 v1 当前归属语义，不能伪造 historical proof。

同一个 confirmed-configuration 握手同时承载 SOP instance report 能力，但保持旧 Edge 的响应形状：旧 Edge 不发送 report capability header，因此新 Center 仍只返回既有 decision v2 字段；新 Edge 显式请求 `sop-instance-report-v1` 后，新 Center 才附加 instance contract version。新 Edge 在发送实例开放快照或在判定后提交实例闭合快照前都必须先确认该能力，因此新 Edge→旧 Center 会在 decision POST 前显式拒绝并保留 outbox，而旧 Edge→新 Center 不受新增字段影响。上述能力握手只覆盖当前 historical decision 与 SOP instance report 的异步升级边界；仓库仍未实现覆盖所有 Center↔Edge 机器接口的软件版本/能力集合的通用启动握手。后续改变其他机器协议时仍须按 ADR-0003 补充对应的显式兼容协商，而不能把配置 bundle 的 contract version 当成 runtime handshake。

改变推理机报告、配置束、物理执行权、状态持久化或恢复语义时，把旧/新中心与旧/新边缘的兼容矩阵作为升级设计的一部分。配置同步相关矩阵应覆盖旧确认值、首次无确认值、无效/旧 revision、中心不可达和恢复后的重新确认；任何新增机器协议版本都必须覆盖不兼容拒绝和显式告警路径。

## NVIDIA 基座更新

NVIDIA 仓库是 NVSOP 的基座躯干。subtree 更新、检查全部已登记补丁、契约验证和独立审查的唯一流程见[仓库维护](../engineering/maintenance.md#nvidia-base-code)。补丁以[实际目录](../base/patches/)为准，不在升级说明中另维护补丁数量。

[基座验证台账](../base/verified-commits.md)记录已验证提交及补丁调整，不代表永久 pin。部署升级还须核对本章要求的镜像、持久化状态、机器协议及回滚组合；仓库契约通过不替代目标环境发布证据。

## 工具链和依赖升级

- Python：更新 `pyproject.toml` / `.python-version` 时，同步 `uv.lock` 并跑受影响 Python/CI 门禁。
- Node/pnpm：更新 `.nvmrc` / `package.json#packageManager` 时，同步 `pnpm-lock.yaml` 并跑 Web format/lint/type/test/build。
- Docker/服务镜像：更新 Compose/Dockerfile 中版本后，验证构建、健康检查、迁移和受影响的集成链路；不要只以镜像能 pull 作为升级成功。
- 配置字段：新增字段要有兼容默认/迁移语义或协同发布计划；删除/改义按破坏性变化处理。

## 发布 promotion 证据

正式发布的部署证据至少覆盖适用的 SBOM/许可证审查、镜像与依赖扫描、迁移/回滚演练、离线启动、不可变镜像/模型摘要，以及[目标环境验证矩阵](../research/target-environment-validation-matrix.md)中适用的现场门禁。升级任务不能用同版本软件测试替代这些证据；所需环境不可用时保持 blocker/gap。

## 发布前最小升级证据

升级任务至少记录：基线 commit、目标 commit、迁移/生成命令、适用测试、兼容矩阵与仍未完成的现场门禁。若备份/恢复或目标硬件验证仍是前置条件，应把它们列为发布 blocker/gap，而不是在文档中推断为已解决。
