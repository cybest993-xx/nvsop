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

**当前仓库没有完成 Q36 备份/恢复策略。** 因此不要在本文声称数据库、MinIO 或边缘 SQLite 已有统一备份周期、自动灾备或经过演练的恢复目标；具体交付前必须完成适用的策略和验证。

## 中心与边缘运行时

边缘运行时/可下载部署包可能与中心非同步升级，因此架构要求它们最终不能依赖 URL 版本隔离，而应通过启动/连接时的版本握手和兼容契约管理差异。ADR-0003 规定的目标语义是：

- 兼容组合正常接入；
- 不兼容组合**拒绝接入并显式告警**；
- 不允许“看起来还能跑”的静默降级；
- 配置同步实现完成后，中心离线期间继续使用最后已确认的可用配置，恢复后按既有对账语义重新连接。

**当前仓库尚未提供这些异步升级保护的完整实现。** Edge 生产入口当前以本地 bootstrap JSON 构建运行时，`apps/edge-runtime` 没有中心配置拉取/原子确认路径；仓库中也没有 center-edge 版本握手路径。因此中心配置同步/最后确认（路线 #44）和版本握手在实现、旧/新组合契约测试及部署证据完成前都是明确的升级 blocker/gap，不能只凭 ADR 或同版本测试宣称兼容。

改变推理机报告、配置束、物理执行权、状态持久化或恢复语义时，把旧/新中心与旧/新边缘的兼容矩阵作为升级设计的一部分；握手/确认机制实现后，矩阵还必须覆盖其拒绝、告警和恢复路径。

## NVIDIA 基座更新

NVIDIA 仓库是 NVSOP 的基座躯干。标准更新命令由 [`../base/verified-commits.md`](../base/verified-commits.md) 维护：

```sh
git subtree pull --prefix=vendor/sop-monitoring-blueprints \
  https://github.com/NVIDIA/sop-monitoring-blueprints.git <ref> --squash
```

更新后：

1. 检查登记的两个补丁是否仍保持原有边界，不能静默扩大修改面；
2. 运行 `make check`，其中包含 `tests/contract/base/`；
3. 处理真实基座行为变化，而不是放宽契约测试隐藏变化；
4. 在 `docs/base/verified-commits.md` 追加 NVIDIA commit、测试结果和补丁调整结论；
5. 完成 `vendor/` 变化要求的独立只读审查。

`verified-commits.md` 是“哪些 NVIDIA 提交验证过”的账本，不代表永久 pin 在最后一条记录。

## 工具链和依赖升级

- Python：更新 `pyproject.toml` / `.python-version` 时，同步 `uv.lock` 并跑受影响 Python/CI 门禁。
- Node/pnpm：更新 `.nvmrc` / `package.json#packageManager` 时，同步 `pnpm-lock.yaml` 并跑 Web format/lint/type/test/build。
- Docker/服务镜像：更新 Compose/Dockerfile 中版本后，验证构建、健康检查、迁移和受影响的集成链路；不要只以镜像能 pull 作为升级成功。
- 配置字段：新增字段要有兼容默认/迁移语义或协同发布计划；删除/改义按破坏性变化处理。

## 发布 promotion 证据

正式发布遵循 [`repository-verification.md` 的目标环境与 release gates](../design/repository-verification.md#target-environment-and-release-gates)。升级任务不能用同版本软件测试替代其中的供应链、迁移/回滚、离线启动、不可变制品或目标环境证据；所需环境不可用时保持 blocker/gap。

## 发布前最小升级证据

升级任务至少记录：基线 commit、目标 commit、迁移/生成命令、适用测试、兼容矩阵与仍未完成的现场门禁。若备份/恢复或目标硬件验证仍是前置条件，应把它们列为发布 blocker/gap，而不是在文档中推断为已解决。
