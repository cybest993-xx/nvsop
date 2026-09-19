# 基座是躯干：一处就地改造 + 序列比对自己实现

NVIDIA 仓库的代码是本系统的躯干，不是外部依赖。姿态分三类：基座已实现且够用的**原样复用**；接近但不够的**就地改造**；基座没有或耦合过深的**自己实现**。不允许出现"基座有一套、我们再写一套并行运行"的重复实现。

## 分类清单

| 姿态 | 范围 |
|---|---|
| 原样复用 | DeepStream 取流、DDM 分段、vLLM 分类、`/v1/*` 接口、文件 API、Prometheus 指标；训练侧 5 个微服务；**React 标注 UI 连界面一起复用** |
| 就地改造（一处） | 同一登记补丁在 `on_message` 输出 source error / delivering / EOS，并在 uniform/DDM chunk 后处理发现 PTS 实际回退时重置分块状态、重新锚定 `source_anchor`。见下节 |
| 自己实现（一处） | **序列比对与周期边界**：在 `apps/edge-runtime/` 重新实现，不打补丁。见下节 |
| 全新建设 | 账户权限、工位/相机/连接器配置、Excel→模板版本发布、聚合看板、违规复核、证据生命周期、上报与对账 |
| 配置关闭（不打补丁） | 基座 checker（`DISABLE_SOP_CHECKER=true`）、基座处置（`ENABLE_ALERT_SOUND`/`ENABLE_MESSAGING` 保持默认 false） |

这一处就地改造保持**纯追加**：只增加健康输出与时间轴观测元数据，不替换 DeepStream / Triton / vLLM 的控制流或计算路径，故 NVIDIA 的性能改进与 CUDA/DeepStream 版本适配仍可 `subtree pull` 进来。

## 为什么处置不再是一处改造

早期方案把"处置：把 `playsound` + Kafka 扩成 5 种动作含写输出点位"列为第二处就地改造。判定移入本机 supervisor 后（[ADR-0005](0005-judgment-runs-inside-the-inference-host.md)）该处不再需要，核实代码后还发现它本来就不合适：

基座的处置在 `post_dispatch_process`（`ds_sop_process.py:800-813`）里，是一个独立线程，对**每个 chunk 无条件**触发 `alert_sound` 与 `messaging_chunk`，**没有违规过滤，也不持有判定结果**。要把它"扩成 5 种动作"，先得让它认识判定结果——而 supervisor 天生就持有判定结果，且 §5.7 本来就把处置派发指派给 supervisor。

两处对外响应面（`playsound`、Kafka）都由环境变量开关，默认关闭，保持关闭即可，无需补丁。

## 为什么序列比对是自己实现而不是打补丁

早期方案是"改造 checker：声明式边界 + 有效性门 + 三值判定"，并声称三处改造都属于"加输出、换边界来源"。核实代码后该说法对这一处不成立：

`missing_number_detector.py` 中，周期边界判断（`:147-188`）与序列比对、状态更新**交织在 `process_number` 同一个方法内**——`_complete_cycle()` 调用、`seen_in_cycle = set()` 清空、`result` 各字段填充全在其中。要把边界来源从启发式换成模板声明的信号，必须整体覆写该方法，无法在外部包一层。即"最小 hook 在 `vendor/`、逻辑在 `apps/edge-runtime/`"这一分工对它做不到。

该文件全文 539 行、只依赖标准库。因此在 `apps/edge-runtime/` 自己实现序列比对与边界求解，`vendor/` 不留补丁。基座那份用 `DISABLE_SOP_CHECKER=true` 关掉——这是基座既有配置（`ds_sop_process.py:88`），置真时 `inference_last_queue`（`:592-598`）直接返回 `_vlm_response_queue`，checker 线程根本不启动（`:652`）。

这不违反"不许两套实现"：我们的判定路径上只有一套（我们的），基座那份在该路径上不被调用。

## 合成健康事件的注入点（已实测）

补丁把健康事件作为一个**合成 chunk** 送进本机流。AC1 要求动作与健康事实保持同一 FIFO 顺序，因此入口必须放在动作最早共用的队列：

| 候选 sink | 结论 |
|---|---|
| `_boundary_queue`（回调当前持有） | 不可用：`clip_post_process` 按 `(frame_id, pts, score)` 位置解包，塞 dict 即崩 |
| `_chunk_queue` | **采用**：动作与健康事实从这里开始共享 FIFO；VLM 开启时 request loop 按 `stream_health` 键旁路推理并原序送入 future queue，VLM 关闭时它本身就是 `inference_last_queue` |
| `_vlm_response_future_queue` | 不直接注入：会越过尚在 `_chunk_queue`、还没提交 VLM future 的更早动作 chunk |
| `_vlm_response_queue` | 不直接注入：会越过已经提交但仍在等待 `response_future.result()` 的更早动作 chunk |

因此补丁仍只触及**一个 vendor 文件且机械上只追加**：`on_message` 把健康事实写入 `_chunk_queue`；VLM request/response loop 各追加一个只匹配 `stream_health` 的旁路分支，正常动作路径不变；`DecodedFrameRetriever.consume()` 在 PTS 回退时清理 internal-vLLM 旧帧；uniform/DDM 后处理同步重置各自时间轴状态。

**E4 实施时的修正**（本 ADR 早期版本记为"两个触点、两个文件"）：早期版本指向 `ds_3d_action_pipeline.py:779` 的 `on_message`，并要求在 `ds_sop_process.py:556` 调用 `create_inference_pipeline` 时多传一个 sink。核实代码后两条都不成立：

- `ds_3d_action_pipeline.py:779` 属于 `ds_boundary_infernce`，只被同文件 `if __name__ == "__main__":`（`:815`）下的命令行入口调用（`:852`），**不在服务路径上**。服务路径的回调是 `ds_sop_process.py:823`。
- 该回调是 `SOPVideoProcessor` 的方法内闭包，`self._chunk_queue` 与 `self.first_timestamp` 在此**已在作用域内**，无需经 `create_inference_pipeline` 传入。第二个跨文件触点因此消失。

E4 修正后补丁先收敛为一个回调触点；S010 又确认两件事：时间轴归零必须与实际 chunk 状态一起处理；健康事实不能越过更早的动作 future。因此同一登记补丁增加 uniform/DDM PTS 回退处理和两个仅针对合成健康键的 VLM 旁路。正常动作的推理/响应控制流不变。

合成事件用**显式键**（`stream_health`）标记，不用哨兵数值。事件与正常 chunk 都携带 `first_timestamp` 作为 `source_anchor`。基座原实现只在初始启动设置该值；登记补丁在 active chunk 后处理看到 PTS 实际回退时重新锚定，并同步丢弃旧时间轴的分块状态；内置 vLLM 的 decoded-frame queue 也在当前恢复帧入队前清掉旧时间轴帧。普通恢复但 PTS 连续不会虚构断裂，真实归零后的下一正常 chunk 才携带新锚点。该值是墙钟身份，只可比对，不可用于计时。

**该通道的边界**：它只在进程与 pipeline 存活时能投递。健康事实和动作从 `_chunk_queue` 起共享 FIFO；VLM 启用时健康事实依次旁路 request/response 计算，VLM 禁用时由该队列直接输出，因此不会越过更早动作。恢复后的时间轴变化仍由随后正常 chunk 的新 `source_anchor` 表达。进程死亡由 supervisor 的 chunk 静默计时器兜底。

**只登记可观测的事实**（E4 实测）：服务路径回调能观测到 `PipelineState.INVALID`（source error）、`PLAYING`（正在投递）与 EOS 三类。"正在重连"**没有**对应的总线消息——基座只给源设置 `init-rtsp-reconnect-interval`，DeepStream 在元件内部重试且不广播，故不设该事实，否则是编造而非观测。重连成功由 `SOURCE_ERROR` 之后紧跟 `DELIVERING` 表达，信息等价。

**EOS 那一类是尽力而为**：EOS 回调与 chunk 后处理结束并发，若 `_chunk_queue` 的 `None` 哨兵先于合成 EOS 入队，后者不会进入 SSE。这不构成损失：SSE 响应本身会结束，supervisor 直接看得到，且流终止本来就由 chunk 静默计时器负责。判定依赖的 `SOURCE_ERROR` 与 `DELIVERING` 发生于流中途，不参与这个结束竞争。

## Considered Options

- **全量只读**：`vendor/` 零改动，中心另建判定与探活。被否决：第一手流健康信号在推理机 pipeline 回调里（[ADR-0005](0005-judgment-runs-inside-the-inference-host.md)），中心只能二次猜测，必然产生两套不一致的实现。标注 UI 也只能在外层包一层 RBAC 或用 Vue 重写，同样是两套。
- **全量吸收（fork）**：低估 GPU 基础设施维护成本。`vss-engine:2.4.1` 与 DeepStream 9.0 的升级适配本身就是持续工作量，分叉意味着自建一个 DeepStream 维护团队，并放弃 NVIDIA 的安全补丁与性能改进。
- **三处都打补丁**（最早方案）：其中序列比对那处必须侵入方法内部控制流，每次 `subtree pull` 都要在他人的状态机里解冲突。
- **两处加输出 + 一处自己实现**（前一版）：把处置列为第二处改造。判定移入 supervisor 后该处失去必要性，见上节。
- **一处登记补丁 + 一处自己实现 + 两处配置关闭（采纳）**：推理基座只保留同一 vendor 文件中的健康事件 hook、internal-vLLM 旧帧队列清理与 uniform/DDM 两条 PTS 回退处理；训练基座另登记一处兼容性补丁，冲突面仍受限；最需要我们掌握的判定核心完全由我们拥有。

## 补丁纪律

- 推理侧就地改造维护为可重放 diff；健康分类逻辑放 `apps/edge-runtime/`，`vendor/` 只保留模块级 hook import、`_chunk_queue` 注入、两个合成健康键旁路、internal-vLLM 旧帧清理和 uniform/DDM PTS 状态重置。这些登记块都不删除基座原行，diff 存于 [`docs/base/patches/0001-stream-health-events.patch`](../base/patches/0001-stream-health-events.patch)，与工作树同步由契约测试保证。
- 训练侧只登记一个兼容性补丁：为 `upload_video` 增加可选的显式 `target_data_id`（省略时保持 `current_data_id` 旧调用兼容），为复用的时间轴输入补上控件标签和可访问名称，增加已签发上下文的独立 React 入口并让标注服务不发布宿主机端口。它不改切片算法、模型路径或存储语义，diff 存于 [`docs/base/patches/0002-annotation-upload-target-and-accessibility.patch`](../base/patches/0002-annotation-upload-target-and-accessibility.patch)，由基座契约测试验证可重放、目标目录隔离、控件可访问性和独立入口。
- **hook 在模块层 import**，不在调用点内 try/except 兜底：`edge_runtime` 不可导入的容器必须在启动时显式失败，而不是照常出流、静默不报健康。基座镜像里 `edge_runtime` 的可导入性属部署期事项（PYTHONPATH 或装包），与 E5 的容器编排一并落地。
- `git subtree pull` 后必跑 `tests/contract/base/`：一类断言验证"我们依赖但不改的基座行为未变"，一类验证"已登记的可重放补丁仍可干净应用且各自约束成立"，一类验证"我们自己实现的序列比对仍与基座在**合规序列**上结论一致"（不含返工与漏步时机——那正是我们故意与基座不同的地方；可跳过步骤不在对比范围内，因为首版不生成该字段）。
- `docs/base/verified-commits.md` 记录每次更新的 NVIDIA 提交号、契约测试结果、补丁是否需要调整。不记录"当前固定在哪个提交"（因为不固定），只记录"哪些提交上验证过"。
- `vendor/` 内两处行为补丁都只依赖基座自身和标准库；推理 hook 与判定核心的标准库约束理由见 [ADR-0005](0005-judgment-runs-inside-the-inference-host.md)。

## Consequences

- `vendor/` 保留两处受控行为改造：推理侧是一处纯追加健康/恢复补丁（同一文件内的健康 hook、队列卫生与两条 chunk 时间轴处理），训练侧是一处兼容性补丁；二者都不是并行重写基座能力。另有 `0003` 非行为清理补丁，仅删除无法由 subtree 携带实体对象的 LFS 文档资产与 tracking 元数据。
- 我们拥有序列比对这段核心算法的维护责任。这不是净增负担：边界求解、有效性门、三值判定本来就要我们写，而它们与序列比对共享同一份状态。
- 基座 checker 与基座处置都靠既有环境变量关闭，不产生补丁。它们在我们的路径上不被调用，故不构成重复实现。
- 仓库策略中"`vendor/` 只读"的表述作废，改为"`vendor/` 只经 subtree 更新或已登记的可重放补丁变更"。
- 训练侧 5 个微服务自带 `metadata_db`，原样复用意味着存在第二个 Postgres。建议合并为一个实例两个 schema：运维一份，逻辑不纠缠，被复用的服务只改连接串。

