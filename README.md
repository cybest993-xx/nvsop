# Factory AI SOP Platform

面向工厂内网部署的商业化 AI SOP 管理与实时监控平台，基于 NVIDIA SOP Monitoring Blueprint 二次开发。

当前阶段：产品与技术架构决策，尚未进入产品代码实施。

- [当前产品方案、技术架构与开发路线](docs/design/solution-and-roadmap.md) — 当前决策的唯一权威来源
- [仓库布局、模块边界、测试与 CI harness](docs/design/repository-harness.md)
- [NVIDIA 基座能力与扩展边界](docs/research/nvidia-base-capability-boundary.md)
- [目标硬件、设备与商业依赖验证矩阵](docs/research/target-environment-validation-matrix.md)

核心技术边界：NVIDIA 基座是本系统的躯干而非外部依赖——能复用的原样复用（含 React 标注 UI 与训练微服务），接近但不够的就地改造（仅两处，均为纯加输出：流健康事件输出、处置动作），基座耦合过深的自己实现（序列比对与周期边界，在 `apps/edge-runtime/` 重写，`vendor/` 不留补丁），基座没有的才新建。**推理机是自治判定单元**：本地完成取流、感知、三值判定、违规锁存、处置与证据缓冲，中心不可达时现场继续工作；凭据与模型也在推理机本地。中心后台采用 FastAPI 模块化单体、基础 Web 采用 Vue 3，是管理面与聚合面，不在实时判定路径上。Nginx 提供统一访问入口但只承载小数据，视频由浏览器直连推理机 MediaMTX。成功实时结果的端到端延迟目标为不超过 500 ms（按结果类型分两个起点），任何超时都必须计数并归因；该能力须在目标硬件上实测。
