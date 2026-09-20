# Center↔Edge 机器契约演进

本文是 Center↔Edge 机器契约兼容策略的权威说明。配置 bundle 的代码权威是 `packages/contracts/src/nvsop_contracts/configuration.py`；历史本地状态迁移只归 `apps/edge-runtime/src/edge_runtime/local_state/configuration.py` 所有。API 固定前缀和不兼容拒绝原则见 [ADR-0003](../../adr/0003-api-v1-is-a-fixed-prefix.md)。

## 配置 bundle：一个当前模型

配置 HTTP wire 只维护一个当前模型。`contract_version` 仍作为 wire shape 完整性标识，但共享 parser 不并行维护 v1/v2/v3 行为分支；普通字段新增不因此创建新 generation。

| 字段/事实 | 所有权与语义 |
|---|---|
| `contract_version` | 当前配置 wire shape；不是软件版本、运行时版本或通用 capability handshake |
| `host_id` | bundle 的主机作用域；Edge 必须与自身签名身份一致 |
| `config_revision` | Center 对该 host 签发的单调 assignment revision；只表达 assignment 次序，不代替运行语义摘要 |
| `generated_at` | 信封生成时刻；诊断元数据，不参与稳定内容或运行语义身份，也不用于同 revision 的先后裁决 |
| `sha256` | 完整 wire 信封的 canonical SHA-256，保护传输/持久化完整性 |
| `effective_sha256` | 运行语义身份；排除 `config_revision`、`generated_at` 和非行为 `producer`，包含会改变 Edge 行为的配置与 capability requirement |
| `station.revision` | `device` 拥有的工位配置修订，不替代 bundle assignment revision |
| 模板 `version_id` / `version_sha256` | `template` 拥有的不可变模板版本身份；artifact 各自保留内容 SHA-256 |
| `model_ids` | Center 签发 bundle 时冻结的推理后端模型事实；历史判定继续使用事件时冻结的 provenance，不用当前配置回填 |
| `producer` | 已知、可选、非行为诊断元数据；不进入 stable/effective identity |
| `required_capabilities` | 行为兼容门禁；排序且去重。Edge 不支持任一声明时显式拒绝整个候选 |

历史 assignment 不再发明第二个 identity。Center 的 issued-configuration 历史以 host + `config_revision` + `effective_sha256` 固定一次签发，并冻结 station/backend、模板版本/摘要和 model facts；历史上报必须指向这份实际签发事实。

## 受控字段演进

允许不增加 contract generation 的变化只有两类：

1. **已知非行为元数据**：先让消费者识别为可选字段，再让生产者发送；不得改变 `effective_sha256`。`producer` 是当前示例。
2. **显式能力门禁的行为扩展**：行为数据必须同时声明对应 `required_capabilities`。不支持的 Edge 明确拒绝，不能忽略字段、猜默认值或走 fallback。

配置契约不提供 `extensions`、任意 JSON bag、插件字典或“未知字段照单全收”。核心字段仍严格必填，未知字段仍拒绝。

只有真正无法用上述方式安全协同发布时才重新定义 contract generation，例如：删除/收窄已存在字段；改变既有字段语义；改变 assignment/effective digest 的身份规则且无法兼容迁移；或 capability gate 无法表达所需的不兼容边界。即使发生这类变化，也应做一次协同迁移并收敛回单一生产模型，而不是长期保留多代运行分支。

## 候选、应用与 durable confirmed

配置同步只有一条生产路径：

1. Edge 拉取候选并验证 wire 摘要、host scope、revision/同 revision 内容冲突及 `required_capabilities`。
2. 旧循环仍运行时，只把候选与主机本地端点、request、adapter profile、凭据解析为纯 `RuntimeConfiguration`；这一阶段不读取/写入 live station supervisor state，SQLite 的 durable confirmed 不变。
3. 纯解析失败时记录诊断，旧 runtime 和最后 confirmed 继续使用；成功时才请求旧循环停机。
4. 旧工位线程全部 join 后，Edge 才从最新 SQLite 状态恢复 supervisor 并构造候选 `RuntimeComposition`。此时不存在旧 supervisor 与候选 supervisor 并发提交；若 composition 失败，重新从原活动 `RuntimeConfiguration` 构造 runtime 并继续最后 confirmed，同时在当前进程内按 `(config_revision, effective_sha256)` 隔离该失败候选。相同候选后续同步不得再次停工位，只有候选身份变化才重新尝试 composition。
5. 候选 composition 构造成功后切换内存 runtime，`LocalConfigurationStore.confirm()` 才原子持久确认同一候选。确认失败会关闭候选工位资源，不启动新 runtime 循环；随后失败退出，重启仍以最后 durable confirmed 为恢复事实。
6. 判定 outbox 的 Center 确认/上报失败沿既有重试路径重发，不触发配置重复应用。

同 revision + 同 effective identity 的 `generated_at`/`producer` 更新可以直接刷新 durable envelope，不重组 runtime；revision 改变即使 effective identity 相同，仍是新的 assignment，必须按新的 revision 确认并用于后续历史归属。

## 历史 Edge SQLite

共享 `configuration_from_wire()` 只解析当前格式。旧持久化兼容只存在于 Edge local-state owner：读取历史 SQLite v1/v2 时，先验证原始 payload 自身保存的摘要，再复制并归一化为当前模型，最后交给共享 parser。只有 v1 中曾明确定义为可省略的 `cameras` / `model_ids` 才可按“未提供 = 空集合”恢复；v2 缺失这些核心字段视为损坏并拒绝，不能推断 backend、模板、模型或其他历史事实。下一次成功确认写回当前 wire。

这条兼容边界不适用于 HTTP：Center 再发送旧 generation 会被当前共享 parser 明确拒绝。

## 现有机器契约清单

| 契约 | 当前形态 | 结论 / 后续所有者 |
|---|---|---|
| Host configuration | `ConfigurationBundle` 当前单一严格模型；行为扩展用 `required_capabilities` | 本文规则适用；历史 v1/v2 仅 Edge local-state 迁移 |
| Decision report | `ReportedDecision` 明确维护严格 v1 与历史证明/复数 provenance 的 v2，并在发送 v2 前通过 confirmed-configuration 握手 | 这是已存在的跨异步升级兼容协议，不由配置契约重构删除；后续演进归 monitor/reporting owner |
| Health report | `ReportedHealth` 严格 `REPORT_CONTRACT_VERSION=1`，status/reason 文本保留可扩展值语义 | 当前无并行 generation；若新增行为语义，先定义对应兼容门禁，归 monitor/reporting owner |
| Delegated connection-test command | 无数字 generation；以严格 `command_type` + 固定字段解析，未知字段拒绝 | 当前单一 shape；需要新命令行为时新增显式 command type/协商，不把任意字段塞入现有 payload，归 delegated-command owner |

Decision report 的 v1/v2 是已有、已握手的报告协议例外，不构成继续给 configuration 增加 v3/v4 的先例。仓库目前也没有覆盖全部 Center↔Edge 机器接口的软件版本握手；每个未来行为扩展必须在自己的协议 owner 处给出明确兼容边界。
