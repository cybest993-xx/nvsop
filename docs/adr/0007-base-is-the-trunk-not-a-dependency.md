# 基座是躯干：一处就地改造 + 序列比对自己实现

NVIDIA 仓库的代码是本系统的躯干，不是外部依赖。姿态分三类：基座已实现且够用的**原样复用**；接近但不够的**就地改造**；基座没有或耦合过深的**自己实现**。不允许出现"基座有一套、我们再写一套并行运行"的重复实现。

## 分类清单

| 姿态 | 范围 |
|---|---|
| 原样复用 | DeepStream 取流、DDM 分段、vLLM 分类、`/v1/*` 接口、文件 API、Prometheus 指标；训练侧 5 个微服务；**React 标注 UI 连界面一起复用** |
| 就地改造（一处） | pipeline `on_message`：把 source error / 正在重连 / 重连成功 / 最后一帧时刻 / 时间轴归零作为**合成健康事件**送进本机流。见下节 |
| 自己实现（一处） | **序列比对与周期边界**：在 `apps/edge-runtime/` 重新实现，不打补丁。见下节 |
| 全新建设 | 账户权限、工位/相机/连接器配置、Excel→模板版本发布、聚合看板、违规复核、证据生命周期、上报与对账 |
| 配置关闭（不打补丁） | 基座 checker（`DISABLE_SOP_CHECKER=true`）、基座处置（`ENABLE_ALERT_SOUND`/`ENABLE_MESSAGING` 保持默认 false） |

这一处就地改造是**纯加输出**，不动 DeepStream / Triton / vLLM 的计算路径，故 NVIDIA 的性能改进与 CUDA/DeepStream 版本适配仍可 `subtree pull` 进来。

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

补丁把健康事件作为一个**合成 chunk** 送进本机流，沿用基座自己的做法——它在流结束时就造过一个 `chunk_idx=-1` 的合成 chunk（`ds_sop_process.py:719-730`）。可用的注入点只有一个，另两个都不可用：

| 候选 sink | 结论 |
|---|---|
| `_boundary_queue`（回调当前持有） | 不可用：`clip_post_process:923` 按 `(frame_id, pts, score)` 位置解包，塞 dict 即崩 |
| `_chunk_queue` | 不可用：`vlm_inference_request_process:1014` 要求数值 `start_time`/`end_time`，并会**对它跑一次 VLM 推理** |
| `_vlm_response_queue` | **可用**：`:1136` 对 `response_future` 有默认值与判空；`DISABLE_SOP_CHECKER` 下它就是 `inference_last_queue` |

因此补丁是**两个触点、两个文件，均为纯追加**：`ds_sop_process.py:556` 调用 `create_inference_pipeline` 时多传一个 sink（`self._vlm_response_queue` 在此处于作用域内）；`ds_3d_action_pipeline.py:779` 的 `on_message` 追加一次对我们 hook 的调用，现有 EOS 分支不动。逻辑在 `apps/edge-runtime/`，`vendor/` 内只留 import 与调用。

合成事件用**显式键**（如 `stream_health`）标记，不用哨兵数值——消费者按键存在与否分支，比认魔数安全，且不与契约测试中针对 `_make_chunk_info` 的断言冲突。

**该通道的边界**：它只在进程与 pipeline 存活时能投递，故能带序送出 source error、正在重连、重连成功、时间轴归零这类可恢复状态；进程死亡送不出任何东西，那种情形由 supervisor 的 chunk 静默计时器兜底。选它买到的是**事件序**（"该事件发生在 chunk N 与 N+1 之间"可直接用于闭合时的有效性判断），不是全覆盖。

## Considered Options

- **全量只读**：`vendor/` 零改动，中心另建判定与探活。被否决：第一手流健康信号在推理机 pipeline 回调里（[ADR-0005](0005-judgment-runs-inside-the-inference-host.md)），中心只能二次猜测，必然产生两套不一致的实现。标注 UI 也只能在外层包一层 RBAC 或用 Vue 重写，同样是两套。
- **全量吸收（fork）**：低估 GPU 基础设施维护成本。`vss-engine:2.4.1` 与 DeepStream 9.0 的升级适配本身就是持续工作量，分叉意味着自建一个 DeepStream 维护团队，并放弃 NVIDIA 的安全补丁与性能改进。
- **三处都打补丁**（最早方案）：其中序列比对那处必须侵入方法内部控制流，每次 `subtree pull` 都要在他人的状态机里解冲突。
- **两处加输出 + 一处自己实现**（前一版）：把处置列为第二处改造。判定移入 supervisor 后该处失去必要性，见上节。
- **一处加输出 + 一处自己实现 + 两处配置关闭（采纳）**：`vendor/` 补丁面只剩一处纯加输出，冲突面最小；最需要我们掌握的判定核心完全由我们拥有。

## 补丁纪律

- 该处就地改造维护为可重放 diff，逻辑放 `apps/edge-runtime/`，`vendor/` 内只留最小 hook（import 并调用我们的包）。它是在回调处追加调用，不进入他人控制流，故该分工成立。
- `git subtree pull` 后必跑 `tests/contract/base/`：一类断言验证"我们依赖但不改的基座行为未变"，一类验证"补丁仍可干净应用且改造行为正确"，一类验证"我们自己实现的序列比对仍与基座在**合规序列**上结论一致"（不含返工与漏步时机——那正是我们故意与基座不同的地方；可跳过步骤不在对比范围内，因为首版不生成该字段）。
- `docs/base/verified-commits.md` 记录每次更新的 NVIDIA 提交号、契约测试结果、补丁是否需要调整。不记录"当前固定在哪个提交"（因为不固定），只记录"哪些提交上验证过"。
- `vendor/` 内那处 hook 只依赖 Python 标准库：它运行在 DeepStream 容器内，版本由基座镜像决定。判定核心同样只依赖标准库，理由见 [ADR-0005](0005-judgment-runs-inside-the-inference-host.md)。

## Consequences

- `vendor/` 冲突面从三处降到一处，且该处是追加调用而非控制流侵入。
- 我们拥有序列比对这段核心算法的维护责任。这不是净增负担：边界求解、有效性门、三值判定本来就要我们写，而它们与序列比对共享同一份状态。
- 基座 checker 与基座处置都靠既有环境变量关闭，不产生补丁。它们在我们的路径上不被调用，故不构成重复实现。
- 仓库策略中"`vendor/` 只读"的表述作废，改为"`vendor/` 只经 subtree 更新或已登记的可重放补丁变更"。
- 训练侧 5 个微服务自带 `metadata_db`，原样复用意味着存在第二个 Postgres。建议合并为一个实例两个 schema：运维一份，逻辑不纠缠，被复用的服务只改连接串。

