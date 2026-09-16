# Factory AI SOP Platform

面向工厂内网部署的视觉 SOP 执行与合规平台：把工厂定义的标准作业转成机器可执行的模板，在现场回答当前在做什么、是否合规、偏离后留什么证据以及触发什么响应。

架构底线：NVIDIA 基座是本系统的躯干；推理机是自治判定单元，中心后台是管理与聚合面，不在实时防错路径上。

## 快速开始

开发实例固定从**主工作树 `main`**运行；任务修改必须从 `main` 新建 `agent/...` 分支和独立 worktree，不直接修改 `main`。

```sh
make hooks
make dev-setup
make dev          # 前台保持运行
```

另开一个终端查看状态或执行测试：

```sh
make dev-status
```

默认业务入口为 `http://localhost:8443`，无需本地 CA。需要本地 TLS 时显式设置 `NVSOP_DEV_PROTOCOL=https`；账号信息见 `.tmp/dev-main/credentials.txt`。完整前置工具、端口、可选 HTTPS CA、日志、refresh/smoke/UI/down 和排障见 [`docs/deployment/development.md`](docs/deployment/development.md)。

## 文档入口

- [`docs/README.md`](docs/README.md) — 按任务查找工程文档的主索引
- [`docs/design/solution-and-roadmap.md`](docs/design/solution-and-roadmap.md) — 产品方案、技术架构、交付路线与验收
- [`docs/deployment/configuration.md`](docs/deployment/configuration.md) — 中心、Web 与边缘运行配置
- [`docs/deployment/limitations.md`](docs/deployment/limitations.md) — 已知限制与仍待现场验证的能力
- [`docs/deployment/upgrade.md`](docs/deployment/upgrade.md) — API、迁移、边缘兼容、NVIDIA 基座与依赖升级
- [`docs/design/repository-harness.md`](docs/design/repository-harness.md) — 仓库规则兼容路由；新工作按其中的直接文档进入
- [`docs/research/target-environment-validation-matrix.md`](docs/research/target-environment-validation-matrix.md) — GPU、相机、连接器、离线、性能与长稳验证

当前优先交付完整软件 MVP，必要验证随实现完成，全量目标环境验证分期推进。交付范围、顺序与退出条件以 [`solution-and-roadmap.md` §八–九](docs/design/solution-and-roadmap.md) 为准，实时进度见 [#1](https://github.com/cybest993-xx/nvsop/issues/1)。
