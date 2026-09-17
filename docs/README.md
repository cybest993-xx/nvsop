# 工程文档索引

本目录维护 NVSOP 的工程事实、设计决策与操作说明。根 [`README.md`](../README.md) 只保留项目入口；需要做具体工作时，从本页按任务进入对应文档，而不是通读整个 `docs/`。

代码、清单、锁文件、部署资产和测试是当前实现事实的最终依据；文档解释稳定边界、操作流程和为什么。一个事实只维护一个主要文档，其他位置链接过去。

| 文档 | 用途 | 什么时候读 |
|---|---|---|
| [`design/solution-and-roadmap.md`](design/solution-and-roadmap.md) | 产品方案、架构决定、交付路线与验收 | 修改产品行为、架构或交付范围 |
| [`design/repository-harness.md`](design/repository-harness.md) | 仓库规则入口与旧 §1–§10 兼容映射 | 不确定仓库规则该去哪里时 |
| [`design/repository-architecture.md`](design/repository-architecture.md) | 仓库布局、模块所有权、依赖与流量边界 | 改目录、模块边界、跨模块依赖或部署拓扑 |
| [`design/repository-authoring.md`](design/repository-authoring.md) | 代码/文档写作、拆分与接口约束 | 实现或重构代码、编写工程文档 |
| [`design/repository-verification.md`](design/repository-verification.md) | 测试选择、证据、命令、CI 与独立审查 | 选择验证、改测试/CI、判断 merge readiness |
| [`design/repository-maintenance.md`](design/repository-maintenance.md) | NVIDIA 基座、生成契约与依赖维护 | 更新 `vendor/`、OpenAPI、依赖或锁文件 |
| [`agents/repository-workflow.md`](agents/repository-workflow.md) | agent 工作树、交接、指令加载与工作循环 | agent 开始/继续/交接一项仓库修改 |
| [`agents/issue-authoring.md`](agents/issue-authoring.md) | 派发 Issue 的类型、范围、验收、依赖与证据规则 | 新建、拆分或重写实施/验证/决策/needs-info Issue |
| [`design/local-branch-workflow.md`](design/local-branch-workflow.md) | `main` 与 `agent/...` 分支的 PR/引用机械规则，含 legacy `dev` 清理 | 创建分支、worktree、PR、合并或清理引用 |
| [`deployment/development.md`](deployment/development.md) | 本地固定 `main` 开发实例安装、启动与排障 | 第一次搭环境或日常运行开发实例 |
| [`deployment/configuration.md`](deployment/configuration.md) | 中心、Web、边缘运行时配置边界与示例 | 部署服务或调整运行配置 |
| [`deployment/limitations.md`](deployment/limitations.md) | 已知产品限制与仍待目标环境验证的能力 | 做交付承诺、现场计划或容量判断 |
| [`deployment/upgrade.md`](deployment/upgrade.md) | 应用、契约、数据库与 NVIDIA 基座升级规则 | 准备升级、兼容性或回滚演练 |
| [`deployment/annotation-evidence.md`](deployment/annotation-evidence.md) | 真实 NVIDIA 标注组合部署验证 | 运行标注专项集成/浏览器证据 |
| [`research/target-environment-validation-matrix.md`](research/target-environment-validation-matrix.md) | GPU、相机、连接器、离线和长稳门禁 | 做硬件/现场验证 |
| [`design/measured-facts.md`](design/measured-facts.md) | 已实测的基座与环境事实 | 需要追溯设计依据 |
| [`adr/`](adr/) | 单项架构决定及取舍 | 需要理解或重新打开既有决定 |

## 文档维护规则

- 产品与领域文档使用中文；agent-facing 指令与操作规则使用英文，代码标识、命令、API 字段保持原样。
- 不把机器可读清单复制成第二份权威。例如中心模块列表以根 `pyproject.toml` 的 `[tool.nvsop]` 为准，运行时版本以 `.python-version`、`.nvmrc`、`package.json` 为准。
- 仍未验证的硬件或现场能力必须写成“待验证”，不能从源码、公开规格或相邻型号外推。
- 被新文档取代的规则应迁移事实并更新入口；Git 历史承担归档，不长期保留两套并行指导。
