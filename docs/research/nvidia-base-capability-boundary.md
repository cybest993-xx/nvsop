# NVIDIA SOP Monitoring Blueprint 能力与扩展边界核验

日期：2026-08-29  
核验基准：[`NVIDIA/sop-monitoring-blueprints@69352021c2aae0ba071acd2629f5cff224d14ca6`](https://github.com/NVIDIA/sop-monitoring-blueprints/tree/69352021c2aae0ba071acd2629f5cff224d14ca6)  
用途：为 Wayfinder 决策票“核验 NVIDIA 基座源码的能力与可扩展边界”提供事实依据。本文只分类事实，不替代后续“裁决基座复用、配置、适配与必要补丁清单”的产品决策。

## 本地基座仓库

- Linux / WSL 路径：`/home/user/sop-monitoring-blueprints`
- Windows 访问路径：`\\wsl.localhost\ubuntu-24\home\user\sop-monitoring-blueprints`
- 已验证远端：`https://github.com/NVIDIA/sop-monitoring-blueprints.git`
- 2026-08-29 验证的 `HEAD`：`69352021c2aae0ba071acd2629f5cff224d14ca6`

后续基座源码核对优先读取该本地仓库；引用结论前仍需记录实际 `HEAD`，避免把工作区变化误当成固定基准事实。

## 结论

基座是**本系统的躯干**。它已经提供视频/RTSP/Basler 输入、DDM 或均匀分段、VLM 动作分类、动作序列比对、流式 API、文件 API、健康/指标、可选 Kafka/声音/视频编码/RTSP 输出，以及动作时间段标注、DDM/VLM 训练和评估服务。以上能力原样复用或配置即可，包括 React 标注 UI 连界面一起复用。

中心后台所需的用户权限、工位/设备/连接器、模板草稿与发布、不可变版本、工位绑定、证据/复核和商业运维均不在基座能力边界内，应由本仓库新建。

推理服务有三个耦合点，它们决定了改造范围（产品裁决见 [`edge-autonomy.md`](../design/mechanisms/edge-autonomy.md) §5.11 与 [ADR-0007](../adr/0007-base-is-the-trunk-not-a-dependency.md)）：

1. `actions.json` 和 VLM prompt 是**进程级文件配置**，不是每个请求或工位携带的模板；同一进程动态承载多个模板没有公开契约。→ 靠部署隔离解决，不改基座：推理后端定义为"承载一份模板配置的进程端点"。
2. 基座检查器只知道动作编号序列，不知道流健康或 SOP 实例的物理边界；周期边界靠启发式推断，**返工序列会被误判为违规**（见下文实测）。→ 序列比对与周期边界在 `apps/edge-runtime/` 自己实现（声明式边界 + 有效性门 + 三值判定），基座检查器按配置关闭，不打补丁改造。
3. RTSP 源设置了"收到错误后的初始重连间隔"，但没有配置"持续无数据后的重连间隔"。服务路径 pipeline 回调能观测状态迁移与 EOS，却原本没有把 `INVALID` / `PLAYING` / EOS 归一为 API 流健康输出；源元件内部重连没有独立总线消息。→ 合成健康事实进入动作共用的 `_chunk_queue`，VLM 开启时按键保序旁路、关闭时直接输出，再经 SSE 到达本机 supervisor。

后两条同时说明**判定必须与推理服务同机**：有效性所需的第一手信号在推理机的 pipeline 里，把判定放在中心只能靠独立探活二次猜测，必然产生两套不一致的实现（[ADR-0005](../adr/0005-judgment-runs-inside-the-inference-host.md)）。

## 方法与证据等级

- **源码核验**：在本地干净检出上逐文件阅读，`HEAD` 为完整提交 `69352021c2aae0ba071acd2629f5cff224d14ca6`。
- **版本差异**：`git diff 6e149568..69352021 -- microservices/sop-inference-bp` 为空；推理服务结论同时适用于先前研究基准 `6e149568`。
- **CPU 实验**：直接导入 `SopCheckerCache`、`SopCheckerRequest`，逐次输入五步序列，未加载 DeepStream、DDM 或 VLM。
- **官方接口交叉核验**：RTSP 属性语义以 NVIDIA DeepStream `nvurisrcbin` 源码/官方插件文档为准。
- **未验证**：没有在 RTX 4090、目标 DeepStream 容器、海康相机或真实多路 RTSP 上运行，因此性能、稳定性和设备行为不在已证实范围内。

## 分类总表

| 能力 | 分类 | 证据与边界 |
|---|---|---|
| 文件、HTTP(S) 视频 URL、RTSP 输入 | 原样复用 | `ChatMessageContent` 接受 `video_url`；pipeline 将 `rtsp://` 交给 `nvurisrcbin`。不等于具备产品级流健康。 |
| Basler 相机输入与模拟 | 原样复用/配置 | `input_camera` 仅声明 `camera_vendor="Basler"`；Compose 默认 `PYLON_CAMEMU=1`。海康应走 RTSP，不应伪装成 Basler。 |
| DDM 边界检测、均匀分段、VLM 分类 | 原样复用/配置 | 推理主链已有；模型、分段和 VLM 参数由环境变量配置。准确率和实时性待 GPU 实验。 |
| 动作序列 missing/misordered 检查 | **自己实现** | 序列比对逻辑 CPU 可运行、可作对比基准；周期边界启发式不可用（返工误判，见下文实测）。边界判断与序列比对交织在 `process_number` 同一方法内，故在 `apps/edge-runtime/` 重新实现并补有效性门与三值判定，基座那份按配置关闭。 |
| OpenAI 风格流式推理 API、文件 API | 原样复用 | `/v1/chat/completions`、`/v1/files*`；产品侧应通过适配器消费，而非把基座模型直接暴露给浏览器。 |
| 健康、元数据、模型列表、Prometheus 指标 | 原样复用 | `/v1/live`、`/v1/startup`、`/v1/ready`、`/v1/metadata`、`/v1/models`、`/v1/metrics`。这些是服务健康，不是每路观测有效性。 |
| Kafka 消息、声音告警、chunk 编码、可选 RTSP 输出 | 配置后复用；**处置为改造起点** | 均由环境变量启用。`playsound` + Kafka 是处置动作的现有全部对外响应面，二者均由环境变量开关且默认关闭；处置由本机 supervisor 实现 5 种动作含写输出点位，基座这两处保持关闭、不打补丁（ADR-0007）。chunk 编码与 RTSP 输出不用于生产预览/证据。 |
| `actions.json`、VLM prompt、模型路径和运行参数 | 配置 | Compose 支持文件挂载和环境变量；配置在进程启动时读取。 |
| 动作时间段标注 UI 与后台 | **原样复用（连界面）** | 基座已有 React + FastAPI 工具，经 Nginx 反代进统一入口、在网关补鉴权即可；不用 Vue 重写。 |
| DDM/VLM 训练、数据增强与评估服务 | **原样复用** | 服务 API 已存在且自带 `metadata_db`；任务治理与审批由中心补齐，模型资产权威留在推理机（不建中心注册表）。 |
| 用户、角色、权限 | 全新建设 | 推理 API 无产品级 AuthN/AuthZ；中心后台必须拥有信任边界。 |
| 工位、相机、推理主机、推理后端、连接器配置 | 全新建设 | 推理服务请求描述输入源，不提供本产品的设备资产与绑定域。 |
| 模板草稿、发布、不可变版本、工位绑定 | 全新建设 | 推理服务只消费进程级动作/prompt 文件和模型路径。 |
| 三值判定、观测有效性、SOP 实例边界、违规锁存 | **自己实现，与信号同机** | 检查器输入没有流健康通道；判定必须与有效性信号同机，否则中心只能二次猜测而产生两套实现。判定核心在推理机的 supervisor 进程内，不在基座进程内（ADR-0005）。 |
| 超时、ROI、外部信号、工业连接器 | 全新建设（运行时在推理机内） | 推理服务没有这些 SOP 业务原语；外部信号先规范化为观测，再与动作编号并列进判定。 |
| 证据、人工复核、数据保留 | 全新建设 | 推理服务可编码 chunk，但没有产品证据/复核生命周期。切片在推理机，归档与复核在中心。 |
| 每工位动态模板或同进程多模板 | **部署隔离，不改基座** | 动作/prompt 是进程级。推理后端定义为"承载一份模板配置的进程端点"，一台推理机可跑多个后端；每后端常驻显存开销须在 G2 门禁单列。 |
| 每路流健康与恢复状态 | **改造 pipeline 回调** | `on_message` 能观测 `INVALID` / `PLAYING` / EOS，但原本未归一为 SSE 健康事实；源元件内部重连没有独立消息。改造为送出真实可观测的合成健康事件，经 SSE 供同机 supervisor 消费。该通道只在进程存活时可投递，进程级失联由 supervisor 的 chunk 静默计时器兜底。 |
| 持续 RTSP 掉流恢复策略 | 配置/验证后再定补丁 | 当前只设置 `init-rtsp-reconnect-interval=10`，未设置无数据重连属性和尝试次数；必须在目标镜像故障注入。 |
| 真并发多流性能测试 | 测试补齐，非产品功能 | `concurrent=True` 明确打印未实现并顺序执行。需自有基准工具或向 NVIDIA 贡献。 |

## 已复现的检查器语义

使用五步动作定义逐 chunk 调用基座检查器，得到：

```text
完整序列 1,2,3,4,5:
  前四步 cycle_completed=False
  第五步 cycle_completed=True

漏步序列 1,2,4,5:
  每一步 missing_detected=[] 且 cycle_completed=False
  keep_alive=False 后 final_missing_detected=[3]

漏步序列后再输入 1:
  新一周期启发式边界触发，missing_detected=[3]
```

这与源码一致：周期边界依靠“重复低号且已完成一定比例”或“高号后出现低号”两条启发式；完整序列仅表示动作号集合齐全。检查器请求只有动作定义、VLM 文本、缓存 id 和三个阈值，没有视频连续性、时间锚定或外部信号字段。因此“真实漏步”和“第 3 步期间不可观察”对检查器是不可区分的相同输入。

### 返工序列被误判为违规（实测）

同一检查器，5 步定义，输入 `1,2,3,2,4,5`——操作员做完前三步后返工重做第 2 步再继续，是一遍完全合规的作业：

```text
输入(1) missing=[]    输入(2) missing=[]    输入(3) missing=[]
输入(2) missing=[4,5]     ← 重复号触发周期边界，seen_in_cycle 被清空
输入(4) missing=[]    输入(5) missing=[]
keep_alive=False → final_missing=[1,3]
```

一遍合规作业被报了两次不合规。根因在 `missing_number_detector.py:149-166`：`number in seen_in_cycle and max_seen > number` 且 `len(seen_in_cycle) >= N × cycle_completion_threshold` 即认定新周期；或 `number <= N × threshold_low` 且 `max_seen >= N × threshold_high` 同样触发。

返工在工厂是常态，故这不是可以靠调阈值回避的边角情况，而是"用动作序列形状推断周期边界"这一方法本身不成立。三个阈值在部署态也不可配：`ChatCompletionRequest` 无对应字段，`ds_sop_process.py:735` 从 chunk dict 读同名键，而全仓无任何代码往 chunk 写这三个键。

结论：序列比对逻辑可用且应复用，**周期边界的来源必须换成模板声明的信号**（[ADR-0006](../adr/0006-cycle-boundary-is-declared-not-inferred.md)）。

来源：

- [`sop_step_checker.py`](https://github.com/NVIDIA/sop-monitoring-blueprints/blob/69352021c2aae0ba071acd2629f5cff224d14ca6/microservices/sop-inference-bp/nvds_action_detector/sop_step_checker.py)
- [`missing_number_detector.py`](https://github.com/NVIDIA/sop-monitoring-blueprints/blob/69352021c2aae0ba071acd2629f5cff224d14ca6/microservices/sop-inference-bp/nvds_action_detector/missing_number_detector.py)
- [`api_types.py` 中的 `SopDetectionRequest/Response`](https://github.com/NVIDIA/sop-monitoring-blueprints/blob/69352021c2aae0ba071acd2629f5cff224d14ca6/microservices/sop-inference-bp/nvds_action_detector/api_types.py#L318-L429)

## 公开接口与隐藏耦合

### 推理接口

推理服务提供：

- `POST /v1/chat/completions`：非流式或 SSE 流式处理视频；
- `POST/GET/DELETE /v1/files*`：上传和管理视频；
- `/v1/models`、`/v1/metadata`：静态模型/服务信息；
- `/v1/live`、`/v1/startup`、`/v1/ready`：进程与模型就绪；
- `/v1/metrics`：请求和 GPU 指标。

输入支持视频 URL、已上传文件和 Basler 相机。基座没有产品级访问控制中间件；出现的 Bearer token 仅用于它调用外部 VLM endpoint。故中心后台必须终止用户信任边界，推理服务仅暴露在受控服务网络中。

运行结果不是 DeepStream 直接返回给浏览器的普通 REST 对象。源码路径是：DeepStream/PyServiceMaker 把帧与 DDM 边界送入本机有界队列，`SOPVideoProcessor` 形成 chunk 并执行 VLM 与 checker，`final_queue` 交给 FastAPI；`stream=true` 时 `/v1/chat/completions` 将每个含 `chunk_metadata` 的结果序列化为 `text/event-stream`。产品应直接复用这个 SSE 契约做薄适配，不再引入新的边缘消息代理。

这条复用只能减少传输层开销，不能证明 500 ms 端到端目标已经满足。chunk 边界形成、DDM 前后文帧、VLM 推理和队列等待均在 HTTP 输出之前；必须用基座已有时间戳与统一单调时钟在目标硬件上分段测量。

来源：[`api_server.py`](https://github.com/NVIDIA/sop-monitoring-blueprints/blob/69352021c2aae0ba071acd2629f5cff224d14ca6/microservices/sop-inference-bp/nvds_action_detector/api_server.py)、[`api_types.py`](https://github.com/NVIDIA/sop-monitoring-blueprints/blob/69352021c2aae0ba071acd2629f5cff224d14ca6/microservices/sop-inference-bp/nvds_action_detector/api_types.py)。

### 模板和模型是进程级配置

`VLM_PROMPT_PATH` 在模块加载时读取到全局 `VLM_PROMPT`；每个 `SOPVideoProcessor` 从同一个 `ACTION_CONFIG_PATH` 读取动作定义。Compose 通过 bind mount 和环境变量注入这两个文件。API 请求没有模板 id、动作定义或 prompt 版本字段。

这意味着首个中心后台切片可以安全地完成“生成、校验、下载基座制品”，但不能宣称已经支持在同一推理服务进程中原子切换任意工位模板。可行的适配优先方案是由部署单元锁定一份模板配置，并让中心后台维护 desired/reported 版本；单进程多模板必须等容量和部署决策后判断是否值得修改基座。

来源：[`ds_sop_process.py#L113-L123`](https://github.com/NVIDIA/sop-monitoring-blueprints/blob/69352021c2aae0ba071acd2629f5cff224d14ca6/microservices/sop-inference-bp/nvds_action_detector/ds_sop_process.py#L113-L123)、[`ds_sop_process.py#L661-L664`](https://github.com/NVIDIA/sop-monitoring-blueprints/blob/69352021c2aae0ba071acd2629f5cff224d14ca6/microservices/sop-inference-bp/nvds_action_detector/ds_sop_process.py#L661-L664)、[`deploy/compose.yaml`](https://github.com/NVIDIA/sop-monitoring-blueprints/blob/69352021c2aae0ba071acd2629f5cff224d14ca6/microservices/sop-inference-bp/deploy/compose.yaml)。

### RTSP 重连不等于流健康

基座给 `nvurisrcbin` 设置 `init-rtsp-reconnect-interval=10`。NVIDIA DeepStream 源码将该属性描述为：RTSP 源收到错误时，等待指定秒数后强制重连。官方还提供另一属性 `rtsp-reconnect-interval`，用于最后一次收到数据后超时重连，以及 `rtsp-reconnect-attempts` 控制次数；基座没有设置后两项。

服务路径 pipeline 回调处理状态迁移与 EOS，但没有把 `INVALID`（source error）、`PLAYING`（delivering）与 EOS 归一为 SSE 流健康业务字段；源元件内部重连没有独立总线消息，恢复后的时间轴变化需比较 `first_timestamp`。故修正旧结论如下：基座不是“完全没有重连”，而是**存在有限的初始错误重连配置，但没有产品所需的可观测流健康契约，持续断流恢复行为也尚未实测**。

来源：

- [`ds_3d_action_pipeline.py#L477-L494`](https://github.com/NVIDIA/sop-monitoring-blueprints/blob/69352021c2aae0ba071acd2629f5cff224d14ca6/microservices/sop-inference-bp/nvds_action_detector/ds_3d_action_pipeline.py#L477-L494)
- [`ds_3d_action_pipeline.py#L772-L790`](https://github.com/NVIDIA/sop-monitoring-blueprints/blob/69352021c2aae0ba071acd2629f5cff224d14ca6/microservices/sop-inference-bp/nvds_action_detector/ds_3d_action_pipeline.py#L772-L790)
- [NVIDIA DeepStream `gst-nvurisrcbin` 属性实现](https://github.com/NVIDIA/DeepStream/blob/c0b76f991f46de0291dd62ae7aaa66690b514a5d/src/gst-plugins/gst-nvurisrcbin/gstdsnvurisrcbin.cpp)
- [NVIDIA DeepStream `nvurisrcbin` 官方文档](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvurisrcbin.html)

## 模型、训练和标注边界

- DDM checkpoint 不随推理仓库提供；缺失时模型初始化明确失败。VLM 路径也无默认值，非 dummy 模式必须提供。
- DDM 默认训练配置是单 GPU、ResNet-50、224 分辨率、每侧 5 帧、batch 4、10 epochs、AMP 关闭。该配置说明“支持单 GPU 训练”，不证明目标数据在 4090 上的时长、显存或准确率。
- DDM 数据读取格式是 `{video_id: segments[]}`，对应 `data_root/{video_id}.mp4`；边界由相邻片段结束/开始时间的中点计算，描述含 `final segment` 的片段被滤除。
- 标注服务是 React + FastAPI，处理 `actions.json`、MP4 上传、时间戳标注、切片，并可触发训练/增强/评估。它适合作为专用工具复用或嵌入统一 Web shell，不应被误认为已有中心后台。

来源：

- [`triton_model_repo/ddm/1/model.py#L245-L265`](https://github.com/NVIDIA/sop-monitoring-blueprints/blob/69352021c2aae0ba071acd2629f5cff224d14ca6/microservices/sop-inference-bp/nvds_action_detector/triton_model_repo/ddm/1/model.py#L245-L265)
- [`ddm_train_config.yaml`](https://github.com/NVIDIA/sop-monitoring-blueprints/blob/69352021c2aae0ba071acd2629f5cff224d14ca6/microservices/sop-training-bp/assets/config/ddm_train_config.yaml)
- [`ddm_dataset.py#L332-L410`](https://github.com/NVIDIA/sop-monitoring-blueprints/blob/69352021c2aae0ba071acd2629f5cff224d14ca6/microservices/sop-training-bp/microservices/ddm-training-ms/ddm/DDM-Net/datasets/ddm_dataset.py#L332-L410)
- [视频标注服务 README](https://github.com/NVIDIA/sop-monitoring-blueprints/blob/69352021c2aae0ba071acd2629f5cff224d14ca6/microservices/sop-training-bp/microservices/video-annotator-ms/README.md)

## 必须保留到真实环境的验证项

以下事实不能由源码阅读替代：

1. RTX 4090 上 DDM、VLM 和全链路的显存、吞吐、chunk backlog 与 p50/p95/p99。
2. 2/4 路真并发；推理服务 `test_multiple_streams(concurrent=True)` 当前明确退化为顺序执行。
3. RTSP 初次连接失败、传输中断、长时间无包、相机重启和恢复后的时间戳行为；分别验证两个 reconnect 属性及尝试次数。
4. SSE 在断流、重连、pipeline error、EOS 和客户端断开时的可区分性。
5. 海康主/子码流编码、B 帧、认证、断流恢复及浏览器预览成本。
6. DDM checkpoint 与选定 VLM 权重的许可证、离线打包和可分发性。
7. Basler Pylon SDK、模型和运行镜像的完整商业许可/SBOM，不以 Apache-2.0 仓库许可证代替逐制品审查。
8. 最后一帧到 Web 渲染的每个成功实时结果是否不超过 500 ms；任何超预算结果都计数并归因。若失败，先定位 chunk/VLM 等推理服务阶段，不以更换 REST/SSE 包装掩盖计算瓶颈。

并发测试证据：[`api_client_perf.py#L584-L625`](https://github.com/NVIDIA/sop-monitoring-blueprints/blob/69352021c2aae0ba071acd2629f5cff224d14ca6/microservices/sop-inference-bp/tests/api_client_perf.py#L584-L625)。

## 改造范围已裁决

“裁决基座复用、配置、适配与必要补丁清单”已由 [`edge-autonomy.md`](../design/mechanisms/edge-autonomy.md) §5.11 与 [ADR-0007](../adr/0007-base-is-the-trunk-not-a-dependency.md) 结案：推理侧就地改造限于同一个 vendor 文件的登记纯追加补丁——pipeline 回调输出流健康，internal-vLLM 在 PTS 回退时先清旧 decoded frame，uniform/DDM chunk 后处理再重置各自旧分块状态并重新锚定时间轴；序列比对与周期边界在 `apps/edge-runtime/` 自己实现，基座 checker 与处置按既有环境变量关闭，其余原样复用或全新建设。

以下门槛用于判断**将来新出现**的改造候选是否越界：

1. 现有公开接口与配置能否实现？能则不改基座。
2. 进程/部署隔离是否足以满足需求？足够则不为抽象完美而改基座（多模板即按此裁决为部署隔离）。
3. 缺口是否属于基座自身通用缺陷（例如并发测试、流状态事件）？是则同时向 NVIDIA 贡献最小改动，减少长期补丁负担。
4. 改动能否维持在"加输出、换边界来源"的性质，不分叉 DeepStream/Triton/vLLM 计算路径？分叉计算路径等于自建 GPU 维护团队。
5. 改动逻辑能否放在 `apps/edge-runtime/`，只在 `vendor/` 留最小 hook？不能则补丁面过大，须重新设计。
6. 目标 GPU/设备实验是否证明必要？缺乏实测证据时保持“候选”，不提前建立长期分叉。
