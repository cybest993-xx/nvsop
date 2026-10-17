# NVSOP 全模块性能测试设计与分期实施

状态：**规划 + 首批 Edge 软件基准实现；尚非目标环境性能验收**。2026-10-16 初版基于 `origin/main@a263dbeef3a11e027574541efef48cf65a3432cc`，交付候选已无冲突更新至 `origin/main@d39a91ce2f10c10ddd16f920a22aea06595096fb`，并核查期间的主线提交没有改动本轮复用的 Edge `advance`、SQLite `commit` 和测试夹具接口。该 SHA 为本次规划的**已检查基线**，并非长期固定版本。

本文负责**现有模块的性能风险排序、可重复场景、指标证据与后续测试顺序**。已有产品目标及现场验收由[方案与路线](../design/solution-and-roadmap.md)、[判定与边界](../design/mechanisms/judgment-and-boundary.md)、[目标环境验证矩阵](target-environment-validation-matrix.md)拥有；本页不改变这些要求。实际运行入口以[交付工作流](../engineering/workflow.md)及[Makefile](../../Makefile)为准，当前已经提供 **`make edge-bench`** 的独立 S1/S2 基线入口，但仍**没有全链路性能验收或目标环境容量结论**；历史单点媒体实测见[已实测事实](../design/measured-facts.md)，不能作为 S1/S2 旧基线。

## 1. 验证层级与不可放宽的门禁

| 层级 | 对象和环境 | 可以证明 | 不得外推 |
|---|---|---|---|
| S：软件微基准 | 纯判定、隔离 SQLite、CPU，合成事件 | 函数/事务耗时、基准波动、代码级回归 | GPU 吞吐或源帧至 Web 实时性 |
| I：真实软件集成 | 隔离 Center + PostgreSQL/TimescaleDB、HTTP/SSE、合成 RTSP/MediaMTX、浏览器 | 数据持久化/消费、并发、背压、积压恢复、媒体与 Web 争用 | 真摄像机、真 DDM/VLM 及目标硬件容量 |
| H：目标环境门禁 | 合法模型权重、指定 GPU、真实相机/连接器、浏览器、网络和低压台架 | 具体设备配置下的实时性、并发、长稳和恢复 | 其他硬件、不同模型或未跑的现场场景 |

- **实时结果的已批准目标仍是每个成功结果 ≤ 500 ms、测试窗口超预算数为 0。** 按[§5.6](../design/mechanisms/judgment-and-boundary.md#56-500-ms-的两个起点)分开计时：“到达即判”从最后必要源帧在相机侧产生起，“闭合类”从闭合条件成立起，终点均为结果完成 Web 渲染；报告 p50/p95/p99、最大值、有效样本数、超预算数。不能以均值或 p99 合格代替每个样本的上限。
- **本地判定与处置不依赖 Center。** Center 离线时 Web 停止更新，不能因此把本地处置与 Center HTTP/SSE 合为一条物理防错路径；本地输入→判定→处置另外计时。监控展示端到端目标的离线适用条件按产品文档解释，不以丢弃样本掩盖不可达。
- **目标矩阵中的 G2/G3/G4/G5/G6、C2/C3/C4、I2、T1/T2 仍是独立门禁。** 真实并发以 **N 个推理后端进程 × 每后端 1 路**为主（1→2→4，每档至少 2 小时）；单路真实流至少 2 小时；稳定性先 72h 再 7 天；预览玻璃到玻璃也有独立的 ≤ 500 ms 约束。纯软件或合成流不能宣称这些门禁通过。
- **正确性前置：** 不可观察区间不能错误输出“不通过”；本地反应事务全部提交或全部回滚；未确认队列的唯一副本不丢弃；恢复对账不重复物理处置；媒体/保留不能绕过已引用证据保护。

## 2. 实际模块、测试接缝与优先级

优先级仅表示**先增加什么性能验证**，不是代码已存在缺陷的结论；现有测试属于功能/契约证据，不是吞吐证明。

| 优先级 | 当前实现与模块归属 | 已有验证 | 增量性能场景 |
|---|---|---|---|
| P0 | NVIDIA 推理基座 `vendor/sop-monitoring-blueprints/microservices/sop-inference-bp/` + Edge [工位 SSE 输入](../../apps/edge-runtime/src/edge_runtime/station_runtime.py) | [基座契约](../../tests/contract/base/test_base_contract.py)、[工位运行](../../apps/edge-runtime/tests/integration/test_station_runtime.py) | DeepStream/chunk、DDM、VLM 单路和真实多后端；逐路时延、chunk 积压、后端常驻显存；**H** |
| P0 | Edge [判定纯函数 `advance`](../../apps/edge-runtime/src/edge_runtime/judgment/core.py)和 [supervisor](../../apps/edge-runtime/src/edge_runtime/supervisor/station.py) | [判定单元测试](../../apps/edge-runtime/tests/unit/test_judgment_core.py)、[supervisor 单元测试](../../apps/edge-runtime/tests/unit/test_supervisor_station.py) | 正常/错步/反序/超时/返工/闭合/不可判定事件轨迹下的单步及长尾；**S** |
| P0 | Edge [SQLite 本地原子状态](../../apps/edge-runtime/src/edge_runtime/local_state/store.py)及[四类待办队列](../../apps/edge-runtime/src/edge_runtime/local_state/queues.py) | [本地状态集成](../../apps/edge-runtime/tests/integration/test_local_state.py)、[容量保护](../../apps/edge-runtime/tests/integration/test_offline_queue_capacity.py) | 一次反应事务耗时、队列积压/确认/排空、磁盘增长、容量失败封闭；**S/I** |
| P0 | Edge [中心报告对账](../../apps/edge-runtime/src/edge_runtime/reporting.py)、[证据上报](../../apps/edge-runtime/src/edge_runtime/evidence_reporting.py)、[连接器](../../apps/edge-runtime/src/edge_runtime/connectors/runtime.py) | [中心断连故障](../../apps/edge-runtime/tests/integration/test_center_link_faults.py)、[证据登记](../../apps/edge-runtime/tests/integration/test_evidence_reporting.py) | 断网保留自治、重连后的 backlog 排空、幂等、信号采集延迟；**I/H** |
| P1 | Center `monitor` [HTTP 上报](../../apps/control-api/src/factory_sop/monitor/adapters/routes.py)及 [Postgres 存储](../../apps/control-api/src/factory_sop/monitor/adapters/repository.py)；TimescaleDB 镜像事实表/原生列存策略 | [TimescaleDB 集成](../../apps/control-api/tests/integration/test_monitor_timescaledb.py)、[运行观测 HTTP](../../apps/control-api/tests/integration/test_runtime_observation_http.py) | 多 Edge 并发入库、幂等重放、历史查询和压缩作业争用；**I** |
| P1 | Center [SSE LISTEN/重放](../../apps/control-api/src/factory_sop/monitor/adapters/streaming.py)（独立有界监听池）、Web [概览](../../apps/control-web/src/modules/overview/OverviewView.vue)与[订阅](../../apps/control-web/src/api/controlPlane.ts) | [SSE 集成](../../apps/control-api/tests/integration/test_monitor_streaming.py)、[反代系统测试](../../tests/system/test_sys_monitor_sse_over_nginx.py) | 连接数/池饱和、慢客户端、Last-Event-ID 重连、推送至 DOM 的延迟；**I** |
| P1 | Edge [证据媒体](../../apps/edge-runtime/src/edge_runtime/evidence_media.py)、[录像压缩](../../apps/edge-runtime/src/edge_runtime/recording_compression.py)，Center `evidence/retention` | [媒体判定隔离](../../apps/edge-runtime/tests/integration/test_media_judgment_isolation.py)、[压缩集成](../../apps/edge-runtime/tests/integration/test_recording_compression.py)、[证据并发](../../apps/control-api/tests/integration/test_evidence_concurrency.py) | 切片、授权读取、保留扫描、压缩与判定争用；**I/H** |
| P1 | Web `overview/devices` 与 MediaMTX/WHEP；[媒体部署](../../deploy/media/README.md) | [浏览器媒体 E2E](../../apps/control-web/tests/e2e/sys-34-media.spec.ts)、[工位运行 E2E](../../apps/control-web/tests/e2e/sys-19-runtime.spec.ts) | 多路预览、首帧、玻璃到玻璃、前端主线程/内存、资源隔离；**I/H** |
| P2 | Center `auth/configuration/device/template/execution`，Web `access/devices/templates` | [执行权集成](../../apps/control-api/tests/integration/test_execution_station_grant.py)、[模板 HTTP](../../apps/control-api/tests/integration/test_template_http.py) | 配置、模板绑定、执行权和身份查询的混合工作负载；先保安全，再按真实热点追加性能基准；**I** |
| P2 | Center `dataset/job`、NVIDIA 训练/标注、Web `datasets` | [数据集并发](../../apps/control-api/tests/integration/test_dataset_action_list_concurrency.py)、[标注数据量集成](../../apps/control-api/tests/integration/test_dataset_annotation_volume.py) | 上传/解析/后台任务与实时服务的 IO、数据库竞争；需独立资源和数据集；**I/H（按需求）** |
| P2 | Center `overview/observability/persistence/retention`、`packages/contracts` | [保留扫描](../../apps/control-api/tests/integration/test_retention_sweep_postgres.py)、[资源 HTTP](../../apps/control-api/tests/integration/test_monitor_resources_http.py) | 汇总、分页、清理批次和序列化仅在剖析出热点后专项测；**S/I** |

辅助模块先作为混合工作负载检查是否与 P0/P1 抢资源，不机械地对每个 CRUD 路由单设套件。

**基座并发陷阱（已由本次主线源码复核）：** [`api_client_perf.py`](../../vendor/sop-monitoring-blueprints/microservices/sop-inference-bp/tests/api_client_perf.py) 的 `concurrent=True` 目前**仍打印未实现告警并顺序运行**。后续 G4 必须用新的独立并发驱动验证同时在途请求和多进程后端，不能把该客户端的“多流”输出当真实并发容量。

## 3. 建议分批增加的可执行场景

| 代码 | 未来场景 | 必报指标 | 失败/安全约束 |
|---|---|---|---|
| S1 | **已实现首批**：使用 `edge_runtime.judgment.core.advance`，复用 `tests/unit/harness.py`；当前测到达即判的顺序跳步（FAIL）与闭合类返工通过（PASS），超时/不可判定等其他样本后续按既有 supervisor 测试补齐 | 每个完整事件轨迹的 p50/p95/p99、最大值及重复批次变异；按结果类型和判定结果分别归类 | 每次重放与 `test_judgment_core.py` 黄金判定一致；**仅正确性断言决定运行成功，S 层没有 500ms 预算** |
| S2 | **已实现首批**：真实文件 SQLite（WAL + `synchronous=FULL`），通过 `open_local_state` → `LocalState.station` → `StationStore.commit`；按未确认积压增量测提交，单列重启重开、待办确认/排空 | 提交耗时按初始预置积压与持续增长的轨迹分组；独立记录重开时间、排空 rows/s、磁盘字节和待办深度 | 关闭重开前后待办数量与已锁存违规一致；容量回滚由原有 `test_offline_queue_capacity.py` 把关；不放宽持久性 |
| I1 | 独立真实 PostgreSQL/TimescaleDB，多个已授权 Edge 客户端同时上报决策、观测、健康与实例并重放 | API p95/p99、入库 TPS、错误率、查询耗时、池占用 | 幂等无重复事实，先持久化确认再释放 Edge 待办；保持授权 |
| I2 | HTTP/SSE + Nginx：多订阅、慢消费者、断线重连与独立监听池饱和 | 建连/首次推送/追赶 p95/p99、活跃连接、池耗尽次数 | 不丢 durable cursor；饱和显式失败，不无界增长连接 |
| I3 | Center 离线期间持续在 Edge 判定/落盘，恢复后排空报告与证据登记 | backlog 峰值、排空时长、失败重试数、最终对账延迟 | 判定/处置不中断，重放不重复处置、不丢证据 |
| I4 | 合成 RTSP + MediaMTX + 真实浏览器；同时切片/播放/保留、扫描与录像压缩 | 首帧/帧率/DOM 延迟、CPU/RSS、磁盘/网络 IO、判定延迟变化 | 预览和证据操作不得反压判定；已引用证据保留 |
| H1 | 真实 DDM/VLM：分别 DDM-only、VLM-only、全链；1→2→4 后端×每后端 1 路 | 两类端到端延迟、每路吞吐/chunk delay、显存/功率/温度及每后端常驻开销 | 按原 G2/G3/G4，真实同时在途，不允许顺序模拟 |
| H2 | 真相机/连接器、故障注入、压缩争用、真实浏览器；先 72h 后 7 天 | 资源与漂移趋势、掉流恢复、错误判定、队列及磁盘增长 | G5/G6、C/I/T 原门禁；跳过项必须保持未验证 |

**探索性而非承诺的负载档：** I 层先从 1→2→4→8 个并发生产者探索，结合现有连接池限制、真实部署需求和资源拐点选定档位；SSE 订阅数、数据量与积压行数也先由环境和测量界定。不得因此写出“支持 N 路、N 用户或固定 TPS”的产品结论。H 层严格按目标矩阵的每后端一路拓扑与时长执行。

## 4. 时钟、结果包与可复现性

1. **身份**：每次记录 commit SHA、工作树是否干净、Python 解释器版本、CPU 型号与可用的调频 governor、OS、真实 SQLite 测试目录及文件系统、后续 I/H 所需的数据库/GPU/驱动/模型 digest 和数据规模。同负载、同环境比较回归，不把版本切换前后的数字混作同一基线。S1/S2 在当前 WSL/CPU 上执行，CPU 调频信息不可读时明确记录空值。
2. **时间**：同一进程使用单调时钟记录阶段耗时；跨相机/主机/浏览器记录源时间、接收时间、锚定与同步偏移及不确定度。**不能直接相减不可比较的主机单调钟**。源帧起点与闭合起点分组，超时/不可判定/断流/丢样单独计数并说明排除理由。
3. **统计**：S1/S2 统一采用 **nearest-rank** 分位数，每个场景每轮预热 100 次、计时至少 1000 次、默认独立跑 3 轮；每轮不足 1000 个样本时 p50/p95/p99 记为 `null` 且标记 `insufficient_samples`。分别报告最大值、均值、批次中位均值和跨批次变异系数（CV>0.20 标记波动较大，不作为性能阈值）。S1/S2 仅通过既有正确性约束判断执行成功，**性能超预算数为不适用**；正式 500 ms 端到端门禁仍用最大值与超预算数裁决，不能用本地微基准代替。
4. **资源**：首批 S1/S2 只记录 CPU/解释器/文件系统环境身份、SQLite pending 深度与数据库字节，尚**未采集**进程 CPU/RSS 或完整资源时间序列；不将未采集值当作 0。后续 I/H 需同时记录 CPU、RSS、GPU 显存/温度/功率、磁盘 IO/水位、网络、句柄、连接池与 TimescaleDB 查询/列存作业，并区分采集/解码、DDM、VLM、判定、SQLite、上报、Center 持久化、SSE 与 Web 渲染等阶段。
5. **证据**：每轮保存 manifest、执行命令/退出码、原始时间序列、聚合分位数、错误与正确性对照、PASS/FAIL/SKIP 及原因。只保存在任务忽略制品目录或受控测试存储；不向 Git 提交客户媒体、密钥、真相机 URI、真实数据库转储或模型权重。
6. **隔离**：S1 使用合成事件；S2 在任务忽略目录 `.nvsop/artifacts/performance/` 下使用真实 SQLite 文件（不是 tmpfs、`:memory:` 或开发实例），保持 WAL/FULL，记录文件系统和数据库大小，结束清理临时数据库；I 使用独占测试数据库/TimescaleDB、独立媒体实例和测试身份；H 使用隔离台架和可逆故障注入，物理输出不连接产线或安全回路。不对生产或共享固定开发实例实施破坏性负载。

## 5. 实施进度与后续增量

每一期都必须先按当时的 `origin/main` 校验 public seam、现有用例和实际部署，再在独立工作树中交付：

1. **F0 首批已实施（Edge）**：使用标准库、现有合成 fixture，确定 nearest-rank、样本数量、预热、重复试验、环境记录及 JSON 输出；尚未覆盖跨进程阶段计时，留给 F2–F4 的实际需要。
2. **F1 首批已实施（Edge 模块拥有）**：新增[可运行脚本](../../apps/edge-runtime/tests/performance/benchmark_edge.py)，复用 `tests/unit/harness.py` 与 `tests/integration/store_harness.py` 的数据及既有黄金判定；覆盖 S1 返工通过/顺序跳步，S2 实体事务、待办重开和排空。未来补充不可判定、空闲闭合、多个深度的长期趋势及独立受控故障；当前脚本**不作为阻塞 CI 或容量门禁**。
3. **F2 Center/TimescaleDB/SSE/恢复**：实现 I1–I3，在真实隔离 Postgres + TimescaleDB、HTTP/SSE、Edge 本地队列上测多客户端与故障恢复；复用 `test_monitor_timescaledb.py`、`test_monitor_streaming.py`、`test_center_link_faults.py`。交付 TPS、长尾、监听配额与排空证据。
4. **F3 媒体/证据/Web**：实现 I4，优先扩展已有 `make media-system`、`make web-e2e` 的合成路径与证据功能回归；交付多路首帧/页面性能/资源争用结果，并明确不替代目标硬件证据。
5. **F4 目标环境**：按目标矩阵准备真正并发的测试驱动及采样工具，在合法模型/真设备就绪后执行 G/C/I/T 适用项、72h→7 天和压缩争用。缺条件则保留阻塞，不能用模拟通过。
6. **F5 回归门禁**：只从低成本且稳定的 S 类基准选择与真实风险对应的回归；I/H 使用受控专项运行入口。要新增阻塞 CI 门禁时，须按[CI 门禁规则](../engineering/workflow.md#ci-gates)将 Make 入口、CI 选择器、required 汇聚与测试一起设计/验证；本轮不修改这些配置。



## 6. 当前环境执行入口与结果解读

使用已有[Make 入口](../../Makefile)：

```sh
make edge-bench
# 快速冒烟（样本数不足 1000，p50/p95/p99 将留空，不能作为稳定容量数据）
make edge-bench PERF_ARGS='--samples 20 --warmup 2 --trials 1 --backlogs 0 3'
```

脚本只依赖仓库已锁定的 Python 环境、标准库和 Edge 真实代码；无需 GPU、摄像机、Center 或 PostgreSQL。默认每场景每轮 1000 个样本、预热 100 次、3 轮，SQLite 在预置积压 0/100 条基础上继续积压（**不是固定队列深度**），单独记录重新打开与确认排空。结果写入忽略目录 `.nvsop/artifacts/performance/edge-baseline.json`，包括提交 SHA、Python/CPU/文件系统身份、逐轮分位数、变异、队列深度、SQLite/WAL 字节、重启与 drain 速率；**不提交到 Git**，不以当前开发机器的具体值设产品容量或 CI 阈值。

本批只覆盖两个有稳定黄金向量的 S1 轨迹与 S2 基线写入/重启/排空；队列容量耗尽的事务回滚仍由[现有集成回归](../../apps/edge-runtime/tests/integration/test_offline_queue_capacity.py)验证。未来新增负载、长稳、Center/SSE/媒体、真实 DDM/VLM 前，应各自确认实际基础设施、独立任务所有权和可复现预算；不得声称本脚本覆盖全部产品验收。

**每期完成定义：** 有复现命令、环境身份、原始指标和错误/安全断言；真实并发被证明；预算与场景相符；设备未到位的门禁明确标记未验证。单元测试、绿色 CI 或模拟媒体不能替代 500 ms、真机容量和长稳发布证据。
