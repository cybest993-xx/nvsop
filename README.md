# Factory AI SOP Platform

面向工厂内网部署的视觉 SOP 执行与合规平台：把工厂定义的标准作业转成机器可执行的模板，在现场回答当前在做什么、是否合规、偏离后留什么证据以及触发什么响应。

当前优先交付完整软件 MVP，必要验证随实现完成，全量验证分期推进。交付范围、顺序与退出条件见 [`docs/design/solution-and-roadmap.md` §八–九](docs/design/solution-and-roadmap.md)，实时进度见 [#1](https://github.com/cybest993-xx/nvsop/issues/1)。

- [当前产品方案、技术架构与开发路线](docs/design/solution-and-roadmap.md) — 产品决策唯一权威，并索引机制规格
- [仓库布局、代码写作规则、测试与 CI harness](docs/design/repository-harness.md) — 代码与验证规则；命令和 CI 门禁仍以此为准
- [Issue tracker 约定](docs/agents/issue-tracker.md) — 分期、依赖、状态证据与关闭规则
- [关键机制设计](docs/design/mechanisms/) — 判定与边界、推理机自治、控制面、证据与保留
- [实测事实](docs/design/measured-facts.md) — 决策依据
- [目标环境验证矩阵](docs/research/target-environment-validation-matrix.md) — GPU、相机、连接器、性能与现场验证证据格式

架构底线：NVIDIA 基座是本系统的躯干而非外部依赖；推理机是自治判定单元，中心后台是管理与聚合面，不在实时防错路径上。实现必须是真实业务逻辑和可运行闭环；测试可在既定 seam 使用合成输入或适配器替身，但不能以占位实现或模拟生产路径结案。
