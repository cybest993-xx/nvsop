# `alert` 并入 `monitor`：推理机上报的镜像只有一个所有者

中心后台的违规归档与处置记录归档不再是独立模块 `alert`，而是 `monitor` 的一部分。`monitor` 因此成为推理机上报的**完整镜像**：SOP 实例、判定、锁存违规、处置记录、流健康。中心模块从 10 个变为 9 个。

本 ADR 推翻 `solution-and-roadmap.md` §六"上报接收面为什么不是一个模块"与 `repository-harness.md` §3 中为 `alert` 独立成模块所写的论证，理由如下。

## 为什么原论证不成立

原论证说三者变更驱动不同：`monitor` 随上报契约变，`alert` 随处置动作种类变，`evidence` 随人工复核变。前后两条站得住，中间那条不成立：

- 处置的类型、锁存与执行结果由推理机拥有（[ADR-0005](0005-judgment-runs-inside-the-inference-host.md)；`Violation`、`WriteOutcome` 定义在 `edge_runtime`）。处置动作种类变化时，先变的是推理机和上报契约，中心收到的仍是同一份上报契约里的镜像行。`alert` 的变更驱动**就是**上报契约，与 `monitor` 相同。
- 二者由同一个上报���口、在同一个请求级 Unit of Work（[ADR-0002](0002-request-scoped-unit-of-work.md)）内写入，读侧也是同一张看板。它们之间没有像 `monitor` / `evidence` 之间那样的分界——机器写入的镜像与人参与的复核流程。

按删除测试看：删掉 `alert`，违规与处置镜像并入 `monitor` 的表，上报入口少写一个模块，`retention` 的引用保护少跨一条缝。复杂度集中了，没有在别处重现——它是一个浅模块。

## 不变的部分

- `evidence` 仍独立：证据引用、再切片请求与人工复核是人参与的流程，生命周期与镜像不同（证据不随录像保留期删除，复核不改写原判定）。
- `retention` 仍独立，仍只拥有策略不拥有数据。它强制的跨模块不变量——不得删除仍被开放违规、在审复核或未结案实例引用的数据——现在横跨 `monitor` 与 `evidence`。
- 违规与处置的执行权威仍在推理机的本地状态；`monitor` 的行是幂等 upsert 写入的报告，不是权威。

## Consequences

- §七 违规域的表改为 `monitor_violation`、`monitor_disposal`，随 C3 首个迁移落地；没有已存在的 `alert_*` 表需要迁移，这是现在而不是 C3 之后做这件事的理由。
- `retention` 解析开放违规时查询 `monitor`，不再查询 `alert`。
- 中心模块清单的唯一声明（根 `pyproject.toml` `[tool.nvsop]`）不含 `alert`，迁移所有权检查的同步副本同样不含，`alert_` 前缀的迁移会被门禁拒绝。
- 上报入口适配层在一个 UoW 内写两个模块（`monitor`、`evidence`）而不是三个。
