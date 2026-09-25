# 架构决策索引

这里记录架构决定及其取舍，不维护实施进度。改动触及某项决定时先读对应 ADR；需要改变决定则明确说明重新打开的原因，并按[文档管理](../engineering/documentation.md)保留决策追溯。编号不因文档整理而重排。

| ADR | 决定 |
|---|---|
| [0002](0002-request-scoped-unit-of-work.md) | 请求级 Unit of Work |
| [0003](0003-api-v1-is-a-fixed-prefix.md) | `/api/v1` 是固定前缀，不是版本轴 |
| [0004](0004-postgres-is-the-job-authority.md) | PostgreSQL 是异步任务权威 |
| [0005](0005-judgment-runs-inside-the-inference-host.md) | 判定运行在推理机内 |
| [0006](0006-cycle-boundary-is-declared-not-inferred.md) | 周期边界由声明确定，而非启发式推断 |
| [0007](0007-base-is-the-trunk-not-a-dependency.md) | NVIDIA 基座是产品躯干与受控补丁边界 |
| [0008](0008-credentials-stay-on-the-inference-host.md) | 凭据留在推理机 |
| [0009](0009-single-channel-perception-limit.md) | 单通道感知的可观测性限制 |
| [0010](0010-alert-merges-into-monitor.md) | 告警归档并入 monitor |
| [0011](0011-annotation-derived-media-gateway.md) | 标注派生媒体的授权网关例外 |
| [0012](0012-media-ownership-and-center-training-files.md) | 运行/证据媒体留在推理机，中心训练素材用本地文件 |
