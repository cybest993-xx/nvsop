# 标注派生媒体经中心授权的 Nginx 入口

## 状态

已采纳；按 S074 的批准范围补充已登记 DDM 训练结果读取。源训练视频的存储/上传前提已由 [ADR-0012](0012-media-ownership-and-center-training-files.md) 调整；本 ADR 的派生媒体授权入口决策仍有效。

## 背景

系统的运行态视频、预览视频和违规证据媒体由拥有它们的推理机提供，不能被复制到中心媒体仓库或经中心通用中继。训练源视频是另一类低频管理资产：按 ADR-0012 通过中心正式上传 API 流式写入 `dataset` 本地持久卷；它不使用本 ADR 的 8444 派生媒体入口。

复用的 NVIDIA React 标注 UI 还需要在浏览器中读取已准备的标注视频、切片和归档。`annotation-backend` 只加入内部 Compose 网络，不能给浏览器一个绕过网关的服务端口；让浏览器直连它会破坏中心会话和数据集权限边界。

## 决策

保留一个明确、有限的例外：标注准备产生的**派生媒体**经 8444 Nginx 媒体入口读取。该入口必须同时满足：

- 先向中心执行 `auth_request`，中心按会话、数据集权限、上下文或提交归属核对资源；
- Nginx 只把中心返回的 `X-Annotation-Upstream-Video-ID` 或 `X-Annotation-Upstream-Clip-ID` 转成内部基座请求，不接受客户端提供的上游 URL 或路径；
- `annotation-backend` 不发布宿主机端口，Nginx 清空 Cookie、Authorization 和其他中心身份头；
- 标注入口只包含标注视频、切片和归档，不承载运行态 MediaMTX 视频，也不承载源视频上传。

### 已登记 DDM 训练结果的有限补充

`dataset` 保存可追溯的训练结果摘要与不可变配置/checkpoint 对象；大型 checkpoint 读取不应成为控制 API 的字节负担。复用同一 8444 数据监听器，但只允许 `/ddm-results/{command_id}/config|checkpoint`：

- 中心每次按当前会话、`dataset.dataset.view`、命令归属及已登记结果授权，返回该命令实际登记的 `X-DDM-Result-Object`；客户端不能选择对象键、文件路径或上游。
- Nginx 只读挂载 `dataset` 文件卷，键须匹配命令下执行独占的候选目录及两种固定文件名；禁止目录浏览、符号链接及其他路径。响应 `Cache-Control: no-store`，支持 Range；失效会话和撤权仍为 401/403。
- 配置/checkpoint 的候选文件先完整写入，登记、命令完成与通用 job 终态在同一租约围栏事务发布。迟到执行不能覆盖获胜对象；提交不确定时先核对精确登记键再清理自己的未发布候选。
- 控制 API 只返回状态、摘要和授权访问 URL，不转发这些结果字节；不新增任意下载代理或通用对象存储生命周期。标注、源训练视频以及推理机运行态/证据媒体的原边界不变。

## 不采用的方案

- **让浏览器直连 `annotation-backend`**：需要公开服务端口或额外跨域鉴权，会形成绕过中心网关的路径。
- **在中心 API 中转发标注派生媒体读取**：把派生媒体读取耦合进产品控制面，并扩大中心故障对标注 UI 的影响。该否决不适用于 ADR-0012 的低频训练源视频上传。
- **本票新增派生媒体对象存储下载契约**：需要新的制品生命周期、签名下载和清理链路，超出本票；未来若媒体吞吐成为瓶颈再单独决策。

## 后果

Nginx 为已授权标注派生媒体承担低频字节转发，为已登记 DDM 配置/checkpoint 承担只读文件访问；运行态/证据媒体仍留在推理机。源训练视频通过普通控制面上传入口流式进入 `dataset` 本地持久卷，不经过本 ADR 的 8444 派生媒体入口。`docs/deployment/nginx-annotation.conf.example`、[仓库架构](../engineering/architecture.md)、[ADR-0012](0012-media-ownership-and-center-training-files.md) 和控制面机制文档共同约束这些不同媒体路径，避免把任一例外误读为全局视频中继许可。

真实 HTTPS、Nginx、基座服务和浏览器行为的部署验证入口见[标注真实部署证据](../deployment/annotation-evidence.md)与[目标环境矩阵](../research/target-environment-validation-matrix.md)；静态接线和中心授权由当前配置、用例和契约测试共同验证。
