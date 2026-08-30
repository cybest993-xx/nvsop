# 实测事实（方案的地基）

本文是 [`solution-and-roadmap.md`](solution-and-roadmap.md) 各项决策的事实依据，按 §2.x 全局序号编排，被机制设计与 ADR 直接引用。

结论标注沿用决策源：**【实测】** 为本机跑过、有数据。完整源码级证据见 [`nvidia-base-capability-boundary.md`](../research/nvidia-base-capability-boundary.md)；目标环境待验项见 [`target-environment-validation-matrix.md`](../research/target-environment-validation-matrix.md)。

---

## 2.1 基座判定层分不清"没做"与"没看到"

基座 `nvds_action_detector` 的 `__init__.py`、`ds_logger.py`、`missing_number_detector.py`、`sop_step_checker.py` **只依赖标准库**，本机纯 CPU 可直接运行。同一份 5 步 `actions.json`，输入动作序列 `[1,2,4,5]`：

| 场景 | 基座输出 |
|---|---|
| 真漏步（操作员跳过第 3 步） | `missing=[3]` |
| 断流（第 3 步期间视频中断） | `missing=[3]` |

**输出完全相同**。基座无任何通道区分二者，且 checker 请求里根本没有流健康字段 → 三值判定（通过 / 不通过 / 不可判定）必须由我们自己实现（§5.2），并需要一条把流健康送出推理服务的通道（§5.11）。

## 2.2 基座的周期边界启发式会把合规作业误判为违规

这是本方案最关键的一条实测。5 步 SOP，操作员做 1,2,3 后**返工重做 2**，再继续 4,5——一遍完全合规的作业：

```
输入(1)  missing=[]        输入(2)  missing=[]        输入(3)  missing=[]
输入(2)  missing=[4,5]  ← 返工触发周期边界，误报漏步
输入(4)  missing=[]        输入(5)  missing=[]
终帧     final_missing=[1,3]  ← 再误报一次
```

一遍合规作业被判了两次不合规。根因在 `missing_number_detector.py:149-166`：见重复号且已见 `N×0.6` 个动作即认定新周期开始，`seen_in_cycle` 被清空；或低号 ≤`N×0.3` 出现在高号 ≥`N×0.8` 之后同样触发。返工在工厂是常态，该启发式在产线上不可用。

→ **`MissingNumberDetector` 不是状态机，是靠两条启发式猜周期边界的有状态序列校验器。** 序列比对逻辑可用且应复用；**周期边界必须换成模板声明的信号**（§5.1、[ADR-0006](../adr/0006-cycle-boundary-is-declared-not-inferred.md)）。

## 2.3 基座对漏步的报出时机达不到实时

逐 chunk 模拟真实流式（漏第 3 步的一遍作业）：

```
chunk1 动作(1)  cycle_completed=False  missing=[]
chunk2 动作(2)  cycle_completed=False  missing=[]
chunk3 动作(4)  cycle_completed=False  missing=[]
chunk4 动作(5)  cycle_completed=False  missing=[]
```

作业已结束，基座完全沉默。根因在 `process_number`：`cycle_completed=True` 的条件是 `len(seen_in_cycle) == N`，漏一步永远凑不满。`missing` 只在两个时机产出：

1. 下一遍作业开始时触发周期边界（实测 `[1,2,4,5,1,2,3,4,5]` → 第二遍开始才吐 `missing=[3]`）；
2. 流结束（`keep_alive=False`，SSE 终帧）→ 吐 `final_missing=[3]`。

对 7×24 常驻流，时机 2 永不发生 → **当班最后一遍的漏步永远不报**。

**更进一步（E3 契约测试实测）**：时机 1 本身也有门槛——触发周期边界的那个重复号要求已见集合达到 `N×0.6`（`cycle_completion_threshold`）。5 步模板只做了 2 步的一遍（如 `1,5`），下一遍开始也**不**触发边界，实测 `missing` 全程为空。即两个时机对稀疏的一遍作业退化为零个：漏得越多，越不报。该断言固化在 `tests/contract/base/test_sequence_comparison_agreement.py`。

声明式边界 + 空闲时限在源头消除这个缺陷（§5.1）。

## 2.4 流健康信号存在于 pipeline，但被丢弃

复核 `69352021` 后修正早期"无重连"结论：RTSP pipeline 给 `nvurisrcbin` 设置了 `init-rtsp-reconnect-interval=10`（`ds_3d_action_pipeline.py:491`）。按 NVIDIA DeepStream 源码，该属性在 RTSP 源收到错误时等待后触发重连；基座没有设置用于"持续无数据"检测的 `rtsp-reconnect-interval`，也没有显式设置重连次数。

关键在于 `ds_boundary_infernce` 的 `on_message` 回调（`ds_3d_action_pipeline.py:779-784`）**只处理 EOS**：source error、正在重连、重连成功、最后一帧时刻、恢复后时间轴归零全部被丢弃，从未进入 SSE。`checker_result.error_message` 也只反映 checker 自身异常。

→ 流健康的第一手信号就在推理机的 pipeline 回调里。**判定必须与它同机**，否则中心侧只能靠独立探活二次猜测——那正是"两套东西"。改造方案见 §5.7。完整证据见 [`nvidia-base-capability-boundary.md`](../research/nvidia-base-capability-boundary.md)。

## 2.5 基座三项能力空白

- **无时间概念**：`deadlineMs`、超时、时限一概不存在，`time.time()` 只测性能。超时违规须由我们从 chunk 的 `start_time`/`end_time` 与主机单调钟自己算。
- **无处置动作**：对外响应面只有 Kafka 发消息（`messager.py`）与 `playsound` 播告警音。
- **无 ROI / 外部信号 / 目标检测。**

## 2.6 检查器阈值在部署态不可配（实测）

`ChatCompletionRequest`（`api_types.py:245`）**没有** SOP 检查阈值字段；`ds_sop_process.py:675` 硬编码 `0.6/0.3/0.8`，:735 从 chunk dict 读同名键，而**全仓无任何代码往 chunk 写这三个键**（已 grep 确认）。`SopDetectionOptions` 只用于内部 checker 服务，不在公开契约上。

→ 部署态阈值恒为默认值，且无环境变量可改。这三个阈值只服务于将被替换掉的边界启发式（§2.2），换成声明式边界后它们不再有意义。模板版本的运行参数只含产品侧可调参数的**默认值**（空闲时限、超时时限、处置策略），生效值的权威见 §5.3；**顺序性声明不是运行参数**。

## 2.7 DDM 权重不随仓库提供，但可自训

`triton_model_repo/ddm/1/model.py` 里写死：

```
"DDM checkpoint not found at {...}. A fresh box has no checkpoint until you
fine-tune/download one. Obtain a DDM-Net checkpoint (e.g. via the SOP Training
Blueprint), place it under MODEL_ROOT_DIR, and set DDM_MODEL_PATH..."
```

`api_server.main()` 在非 DUMMY 模式下调 `wait_for_model_ready()` → **没有 DDM 权重，服务起不来**。

训练程序支持单 GPU 配置：`ddm_train_config.yaml` 为 `num_gpus: 1`、`resnet50`、`resolution: 224`、`frames_per_side: 5`、`batch_size: 4`、`epochs: 10`、`amp: false`。这不等于已证明单张 4090 的峰值显存、训练时长或准确率；须按目标环境验证矩阵实测。

**DDM 标注格式**（实测 `ddm_dataset.py:330-395`）：

```json
{ "<video_id>": [ {"start_timestamp": 0.0, "end_timestamp": 6.67, "description": "..."}, ... ] }
```

整段视频需在 `data_root/{video_id}.mp4`；边界 = 相邻片段 `end_timestamp` 与 `start_timestamp` 的中点 × fps；描述含 "final segment" 的片段被过滤。

**本地样本数据可转换**（实测）：`server_fan/raw/` 有 12 个整段视频，`server_fan/train/Install_N/` 是切好的片段。以 Install_8 为例：raw 视频时长 71.54s，11 个片段时长合计 71.54s，差值 **0.00s（0.0%）**——片段是整段视频的完整无重叠切分。

**⚠️ 实现期必须实测的坑**：文件名格式 `{序号}_{视频}_{周期}_{动作号}.mp4`，序号 `10` 出现两次（`10_Install_8_1_1.mp4` 与 `10_Install_8_2_11.mp4`，后者周期=2）→ **按文件名排序 ≠ 时间顺序**。定序难题移交给标注环节的人（§5.4）。

样本数据自带的 `train/actions.json` 与 `augmented/{gqas,bcq,mcq,golden_gqa}` 是 **VLM 微调格式**，不是 DDM 格式。

## 2.8 视频链路成本（本机 10 核实测）

| 操作 | 单路 | 8 路并发 |
|---|---|---|
| CPU 解码（1280×592/29fps） | 0.42s / 23.24s 视频 = **55× 实时** | 2.58s / 186s = **72× 实时** |
| CPU 解码 + 缩放 224 | 37× 实时 | — |
| NVDEC 硬解（GTX 960） | 1.48s = 16× 实时 | — |
| **x264enc 转码**（1080p，推理服务 `SW_ENCODER` 确切参数 `superfast zerolatency bframes=0`） | 3.92s / 20s = **5.1× 实时**，占 ~0.2 核 | 14.96s / 160s，跟得上但吃 **~10 核** |
| **零转码 passthrough** | 0.17s = **121× 实时** | 0.21s = **773× 总吞吐** |

→ 解码取流不是瓶颈；**转码才是**。8 路软编吃 10 核，50 路需 ~60 核，不可行。

## 2.9 推理服务预览/录像路径都碰 GPU

- RTSP 输出分支：`SW_ENCODER=true`（compose 默认）走 `x264enc`（CPU 编码），`false` 走 `nvv4l2h264enc`（GPU NVENC）；两者均 `bframes=0`。但**前置必经 `nvvideoconvert`（带 `gpu-id`）**，占 GPU 显存与转换算力；且与判定支路共享 pipeline，有反压风险。
- `ENCODE_VIDEO`：对**每个 chunk 无条件编码**，无违规过滤；`encode_video_gst` 内部**写死 `nvv4l2h264enc`**，`SW_ENCODER` 管不到 → 7×24 持续 GPU 编码 + 磁盘无上限增长。

→ 两条都不能用于生产预览/证据。预览与录像走独立的 mediamtx 路径（§5.5）。

## 2.10 基座其他既定事实

- **API**：OpenAI 兼容，11 端点（`docs/openapi.json` 随仓库提交）：`POST /v1/chat/completions`（SSE，`chunk_metadata` 携带 checker 结果）、`/v1/files` CRUD、`/v1/models`、`/v1/metadata`、`/v1/live`、`/v1/startup`、`/v1/ready`、`/v1/metrics`。输入三形态：`video_url`、`input_video`(file_id)、`input_camera`(Basler)。分段：`ddm-net` 或 `uniform`（后者绕过 DDM）。
- **chunk 可用字段**：`chunk_idx`、`start_time`、`end_time`、`cv_boundary_score`、`cv_execute_time`、`first_timestamp`、`pipeline_starting_timestamp`、`pipeline_cv_ready_timestamp`、`checker_result`。
- **模板与 prompt 是进程级配置**（实测）：`ACTION_CONFIG_PATH` 在 `ds_sop_process.py:113` 于模块加载时读入，每个 `SOPVideoProcessor._initialize_checker()`（:662）从同一路径读同一份文件；API 请求无模板 id 字段。→ **一个推理服务进程只承载一份 SOP 模板**，这直接决定部署形态（§5.10）。
- **镜像**：`nvcr.io/nvidia/blueprint/vss-engine:2.4.1`（NGC 门禁）+ `deepstream:9.0-triton-multiarch` + 商业 Basler Pylon SDK。容器内 Python 版本由 DeepStream 9.0 基础镜像决定，不由我们选择（§5.11 的依赖约束由此而来）。
- **安全**：官方明写无 AuthN/AuthZ、明文 HTTP、单租户、无速率/并发限制。
- **训练侧**：5 个微服务（标注 8100、增强 5487、CR 微调 32080、DDM 微调 32100、评估 32090）+ **自带 `metadata_db`（Postgres）与 Adminer 8080**；Cosmos 全量微调官方参考 `4 × A100 80GB`。
- **agentic/**：三个 Claude 插件包（`ds-sop-skills`、`sop-agentic-ft` 6 个 skill、`vss-sop-skills`），随 subtree 进仓库即可用。

## 2.11 mediamtx 选型依据

不做重编码（瓶颈在带宽非 CPU）；支持 `sourceOnDemand` / `runOnDemand`（仅在有客户端请求时拉源流）；内置分段录像 + 保留策略 + 回放服务 + 快照提取；**WebRTC 不支持 B 帧**。

海康子码流是否无 B 帧**未经查证**（详细编码规格需登录官方文档），只查到"低码率、延迟稳定"。此项列为拿到真机首验项（§5.5），不作为已知事实使用。

## 2.12 piko `authoring_worker` 是空壳

三个文件共 130 字节，`__main__.py` 导入的子模块不存在 → `ModuleNotFoundError`，README 声称的 `--check` 跑不起来。无可复用代码。
