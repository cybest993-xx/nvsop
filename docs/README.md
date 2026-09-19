# 工程文档索引

从任务进入对应文档，不需要通读整个目录。根 [README](../README.md) 是项目和启动入口，[AGENTS](../AGENTS.md) 为 agent 按任务路由，[CLAUDE](../CLAUDE.md) 仅指向该入口。[CONTEXT](../CONTEXT.md) 定义领域术语。

代码、清单、锁文件、部署资产和测试说明当前实现；批准的产品规格和 ADR 说明必须达到的行为。二者不一致时须区分过时说明、尚未实现的要求和实现缺陷，不能通过改文档取消需求。

## 开发与交付

| 要做什么 | 唯一规则归属 |
|---|---|
| 建立任务 worktree、选测试、审查、发 PR、看 CI、合并与清理 | [交付工作流](engineering/workflow.md) |
| 新建、拆分、领取、分派或关闭 Issue，处理标签和依赖 | [Issue 管理](engineering/issues.md) |
| 编码、复用、接口设计、拆分模块和注释 | [代码写作](engineering/coding.md) |
| 改仓库布局、模块所有权或跨模块依赖 | [仓库架构](engineering/architecture.md) |
| 新增或调整仓库本地生成/忽略状态，更新 NVIDIA 基座、生成契约、依赖与锁文件 | [仓库维护](engineering/maintenance.md) |
| 新增、更新、合并、迁移或删除文档/agent 指令 | [文档管理](engineering/documentation.md) |

本地文档迭代可运行 `make docs-check`；最终检查与独立审查条件见交付工作流。命令的实际实现由根 [Makefile](../Makefile) 维护，不在各文档复制完整检查清单。

## 产品与设计

| 文档 | 负责什么 |
|---|---|
| [方案与路线](design/solution-and-roadmap.md) | 产品目标、批准范围、未决问题、软件/现场分期、原始验收 |
| [判定与边界](design/mechanisms/judgment-and-boundary.md) | 声明式边界、三值判定、原因码、延迟和基座契约 |
| [推理机自治](design/mechanisms/edge-autonomy.md) | 本地运行、连接器、凭据、基座 hook、模型与物理执行权 |
| [控制面](design/mechanisms/control-plane.md) | 配置、模板、Web、中心契约、异步任务和模块职责 |
| [机器契约演进](design/mechanisms/machine-contract-evolution.md) | Center↔Edge 配置契约字段所有权、兼容演进、能力门禁与历史持久化 |
| [证据与保留](design/mechanisms/evidence-and-retention.md) | 预览录像、证据片段、复核、保留与压缩 |
| [ADR 索引](adr/README.md) | 单项架构决定、取舍与重新打开决定的依据 |

## 部署与验证

| 文档 | 使用时机 |
|---|---|
| [固定开发实例](deployment/development.md) | 安装、启动、刷新、日志与排障；任务修改仍在独立 worktree |
| [运行配置](deployment/configuration.md) | 中心、Web 和边缘运行时配置来源与同步边界 |
| [已知限制](deployment/limitations.md) | 做产品承诺、现场计划或容量判断 |
| [升级与兼容](deployment/upgrade.md) | 协同升级、迁移、回滚和发布证据 |
| [标注部署证据](deployment/annotation-evidence.md) | 真实 NVIDIA 标注服务、网关和浏览器专项验证 |
| [媒体部署说明](../deploy/media/README.md) | 合成 RTSP、MediaMTX、CPU 转码和本机媒体配置应用 |
| [目标环境验证矩阵](research/target-environment-validation-matrix.md) | GPU、真相机、连接器、离线、多机、容量和长稳门禁 |

## 事实与来源

| 文档 | 负责什么 |
|---|---|
| [已实测事实](design/measured-facts.md) | 带证据出处的设计前提，按记录的提交和环境解读 |
| [基座能力核验](research/nvidia-base-capability-boundary.md) | 指定 NVIDIA 提交的能力研究，不替代后续产品决策 |
| [基座验证台账](base/verified-commits.md) | 验证过的上游提交、受控补丁及已知上游问题 |

文档维护规则集中在[文档管理](engineering/documentation.md)，每项事实维护一个主要位置。任务当前状态与依赖留在 GitHub，旧实施计划迁移有效事实后由 Git 历史保留，不形成第二套现行指导。
