# 推理机重启与恢复对账验证包（S108，Issue #195）

本文件负责 **S108「Edge 重启与恢复对账演练」的离线验证准备包**：如何用只读 CLI 在本机
采集真实证据、如何按场景比较前后快照、需要人工记录哪些现场事实，以及如何归档。

**本文件不声称任何现场通过。** Issue #195 的原验收（AC1/AC2/AC3）在本包中 **全部保持未验收**：
`compare` 只核对离线数据能证明的条件，其余明确列为现场项；合成测试与 fake `psql` 只验证
本机适配器，不能替代真实 GPU、真实相机、真实宿主重启与真实 PostgreSQL。

术语见 [CONTEXT.md](../../CONTEXT.md)；目标环境门禁见[目标环境验证矩阵](../research/target-environment-validation-matrix.md)；
本机运行配置见[运行配置](configuration.md)；推理服务部署入口见[推理机推理服务部署](../../deploy/edge/README.md)；
自治边界见[推理机自治](../design/mechanisms/edge-autonomy.md)。

## 工具与边界

CLI：`scripts/validate_edge_restart.py`，只用标准库，可离线运行，不需要在线下载。它：

- **只读**采集：目标 SQLite 以 `file:...?mode=ro` + 单一读事务打开（不使用 `immutable=1`，
  因此活动 WAL 中的行同样可见）；不写 SQLite、不写 Center、不触发业务判定。
- Center 通过系统已有的 `psql`/libpq 外部 secret 环境读取；SQL 经 stdin（`psql -f -`）传入，
  `-w` 永不提示密码，**不在命令行传 DSN**，不记录 stderr（可能含连接串片段）。
- 只输出白名单字段与摘要：不输出配置原始 payload、相机 URI、secret 路径、模型权重、媒体字节或
  自由文本 `last_error`/`result_detail`；标识/枚举/摘要原样保留，其余字符串只留 sha256，证据文件
  只留「相对路径 sha256 + 内容 sha256 + 大小」。
- 结果默认写到仓库内 `.nvsop/artifacts/edge-restart-validation/`（可用 `--output` 指定外部目录）；
  结果文件 **独占创建，拒绝覆盖**；`--output` 不得等于输入库或落在证据目录内。

两个子命令：`snapshot`（采集）与 `compare`（比较）。

## 前提

- 现场操作者能读取目标推理机的 `local_state_path`（SQLite）与 `evidence.evidence_directory`
  （证据目录）且确有读权限；采集期间 **不要让 edge 运行时同时迁移或写入该库** 以外的清理动作。
- Center 采集需调用方预先配置 libpq 外部 secret 环境（例如 `PGHOST`、`PGDATABASE`、`PGUSER`，
  以及经 secret 文件导出的 `PGPASSWORD`）；本工具 **不生成、不保存、不回显** 任何密码或 DSN。
- 机器上存在 `psql`。缺少它或 libpq 环境不完整时，Center 部分标记为 `unavailable`，命令以非零退出。
- 候选身份：记录 40hex 候选 SHA、主机身份（`local_config.host_id` 必须与显式 `--host-id` 一致，
  否则拒绝，避免采错库）。
- 环境身份：镜像 digest、模型/后端身份、模板版本与 sha256。黄金视频须给出 sha256；这些是现场
  身份，不由本工具推断。
- 真实 PostgreSQL 上的 SQL 是否成功 **尚未在真库运行验证**；fake `psql` 只验证适配器。

## 采集命令

```sh
# 每阶段前后各采一次；before/after 用同一 --host-id、--candidate-sha
python3 scripts/validate_edge_restart.py snapshot \
  --role before|after \
  --sqlite /var/lib/nvsop/edge.sqlite3 \
  --evidence-dir /srv/nvsop/evidence \
  --host-id <推理机 UUID> \
  --candidate-sha <40hex> \
  --environment <环境身份标签> \
  [--center] \
  --output /path/to/<stage>-before.json
```

退出码：

| 退出码 | 含义 |
|---|---|
| `0` | 采集完整（含 Center） |
| `2` | 明确拒绝：schema 不在 [CLI 支持集合](../../scripts/validate_edge_restart.py) `SUPPORTED_SCHEMA_VERSIONS` 中，或输入不合法/输出目标被拒（已存在、落在证据树、等于输入库），或本地 host_id 与 `--host-id` 不一致 |
| `3` | 采集不完整：未请求 `--center`，或 `psql`/libpq 不可用；已采集部分保留 |

## 比较命令与场景

```sh
python3 scripts/validate_edge_restart.py compare \
  --scenario restart|replay|rebind \
  --before <stage>-before.json --after <stage>-after.json \
  [--output /path/to/<stage>-compare.json]
```

`compare` 只依据两份快照中的原始事实判断，**不接受任何人工布尔或快照自报的 pass**；输出含
`scenario`、每条 `checks[].status`（`pass`/`fail`/`needs_field`）、`data_checks`
（`passed`/`failed`/`incomplete`）、`field_acceptance: not_asserted` 和 `needs_field` 列表。

| 场景 | 前提 | 核对 | 明确不要求 |
|---|---|---|---|
| `restart` | 活动实例在进程/宿主重启后自然关闭 | 活动实例以 `RUN_INTERRUPTED`（`INDETERMINATE`）结案；最后确认配置 revision+sha256 不变；旧待办/终态身份、冻结来源、媒体、已终态处置不变；Center 既有镜像身份与不可变来源保留 | 不禁止合法新事件、新实例、新证据；不区分同一实例开/闭两条 fact |
| `replay` | **现场先暂停新输入**；`before` 必须在“首次投递已提交副作用但确认响应丢失、本地仍 pending”之后采集（见下） | 受控主机在两份快照间 **不出现任何新镜像身份**（全部白名单表，含 evidence）；既有镜像身份与不可变来源不变；本机旧待办身份不丢、来源未被替换；本机处置/证据待办身份集不增长 | 不要求活动实例被关闭，不改变配置 |
| `rebind` | 历史改绑/换绑，最后确认配置允许变化 | 旧待办冻结来源不被新归属替换；既有终态处置结果与尝试次数不变；媒体仍在且摘要不变；Center 既有镜像身份与不可变来源保留 | 不要求活动实例被关闭，不要求配置不变；允许合法新事件/新实例 |

退出码：`0` 数据核对通过（仍不宣告 AC 通过）；`1` 有数据核对失败；`2` 输入不合法/快照不可比
（schema 不兼容、主机身份或候选 SHA 不一致）；`3` 不完整（Center 或最后确认配置缺失，相关项
列为 `needs_field`）。

### replay 前提（固定确认，不是首次投递）

replay 针对的是“提交后响应丢失”造成的重复确认，不是首次投递：`before` 必须在目标事件已**在
Center/证据/处置端提交了副作用**、但确认响应丢失且本地行仍为 pending **之后** 采集。首次投递
本身会合法新增 Center 对象与本地待办，因此 **不能拿“首次发送前”的快照去做 replay 比较**，否则
会把正常的首次投递误判为失败。现场做法：停止新输入与新管理命令，只重放这组“已提交副作用”的
固定事件，先等既有媒体/再切片/处置副作用稳定，再采 `after`，核对重复确认不新增身份。故障注入
网关丢的是**提交后的响应**，不是请求；不需要新增自动 fault 工具或新场景。

判定 `needs_field`（始终要求人工现场记录，离线数据不能证明）：

- `physical_execution_count`：处置点位 **物理执行次数**。本机 `local_disposal.attempts` 只是
  去重账本里的逻辑尝试次数，不证明执行器被驱动了几次。
- `protocol_rejection`：协议不兼容时的显式拒绝。需 Center↔Edge 协商日志与拒绝码。
- `real_host_reboot`：真实宿主重启本身。需系统启动记录/uptime 与守护进程重启日志。

## 分阶段采样

按下表逐阶段做 `snapshot` → 人工故障/恢复 → `snapshot` → `compare`，并保存原始脱敏日志：

1. **断网积压**：切断中心—推理机网络，观察本机判定/处置/证据继续、待办增长；恢复后对账。
   记录各队列深度与恢复时间。
2. **进程重启**：停 edge 运行时（由守护进程重启），确认在飞实例以 `RUN_INTERRUPTED` 结案、
   待上报保留原模板/模型/后端来源、最后确认配置继续有效 → `compare --scenario restart`。
3. **宿主重启**：真实重启推理机，恢复后重复进程重启的采集与比较；同时记录宿主启动证据
   （`real_host_reboot` 现场项）。
4. **丢失/重复确认**：**先暂停新输入与新管理命令**；确认目标事件已在 Center/证据/处置端提交
   副作用、仅确认响应丢失且本地仍 pending 后再采 `before`，只重放这组固定事件，确认镜像与本机
   处置/证据身份不新增 → `compare --scenario replay`。协议不兼容场景另行记录拒绝日志。
5. **历史改绑**：在旧待办仍存在时改绑工位/后端，确认旧来源未被新归属替换、终态处置与媒体保留
   → `compare --scenario rebind`。
6. **协议拒绝**：制造 Center↔Edge 契约不兼容，记录显式拒绝而非静默降级（现场项）。

## 台架安全与手动故障范围

- 只在**低压合成台架**上做手动故障；输出点位不接产线设备或安全联锁回路（摄像机继电器不承担
  安全功能）。故障注入必须可逆、可停止，且不影响生产网络。
- **停止条件**：出现非预期物理动作、无法停止的故障、判定/处置出现错误 `不通过`、磁盘或数据
  面临丢失风险时，立即停止并先修复，不继续采样。
- 不要为了造场景而删除未确认的唯一证据副本或清理运行中的队列。

## 失败判据

- `snapshot` 退出 `2`：schema 不受支持或输入被拒，停止并按对应版本/输入修正，不要用旧库继续。
- `snapshot` 退出 `3`：Center 未采到；先修 libpq 外部 secret 配置或 `psql`，否则不得声称完整证据。
- `compare` 退出 `1`：某条 `checks` 为 `fail`，逐条定位（身份丢失/来源被替换/媒体缺失或摘要变化/
  终态处置或尝试次数变化/Center 不可变来源变化/replay 出现新镜像身份）。
- `compare` 退出 `3`：`needs_field` 未满足，属现场记录缺口，不是通过。
- `compare` 通过也 **不等于** AC 通过；AC 由 Issue #195 的现场验收责任方判定。

## 归档

- 每个阶段保存：`snapshot`/`compare` 的 JSON、原始脱敏日志、手工计数结果与操作记录。
- 结果目录默认 `.nvsop/artifacts/edge-restart-validation/`，或操作者指定的外部目录；**禁止覆盖**
  既有结果，禁止扫目录自动清理。
- 脱敏：归档物不得含凭据、DB DSN、相机 URI、配置原始 payload、媒体字节或模型权重；原日志若含
  敏感内容须先在其自身路径脱敏后再归档。
- 记录环境身份（镜像 digest、模型/后端身份、模板版本与 sha256、黄金视频 sha256）与候选 SHA，
  使结果可复现。

## 未验证项

- 真实 PostgreSQL 上的 SQL 实际执行（仅经 fake `psql` 验证适配器与脱敏，未连真库）。
- 真实 GPU/相机、真实 MediaMTX 录像与真实协议拒绝。
- 真实宿主重启与守护进程行为。

以上未验证项在完成对应现场门禁前不得标记为通过；本文件只描述准备与核对方法。
