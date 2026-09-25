# 标注派生媒体经中心授权的 Nginx 入口

## 状态

已采纳，适用于 Issue #31 的标注 UI。源训练视频的存储/上传前提已由 [ADR-0012](0012-media-ownership-and-center-training-files.md) 调整；本 ADR 的派生媒体授权入口决策仍有效。

## 背景

系统的运行态视频、预览视频和违规证据媒体由拥有它们的推理机提供，不能被复制到中心媒体仓库或经中心通用中继。训练源视频是另一类低频管理资产：按 ADR-0012 通过中心正式上传 API 流式写入 `dataset` 本地持久卷；它不使用本 ADR 的 8444 派生媒体入口。

复用的 NVIDIA React 标注 UI 还需要在浏览器中读取已准备的标注视频、切片和归档。`annotation-backend` 只加入内部 Compose 网络，不能给浏览器一个绕过网关的服务端口；让浏览器直连它会破坏中心会话和数据集权限边界。

## 决策

保留一个明确、有限的例外：标注准备产生的**派生媒体**经 8444 Nginx 媒体入口读取。该入口必须同时满足：

- 先向中心执行 `auth_request`，中心按会话、数据集权限、上下文或提交归属核对资源；
- Nginx 只把中心返回的 `X-Annotation-Upstream-Video-ID` 或 `X-Annotation-Upstream-Clip-ID` 转成内部基座请求，不接受客户端提供的上游 URL 或路径；
- `annotation-backend` 不发布宿主机端口，Nginx 清空 Cookie、Authorization 和其他中心身份头；
- 入口只包含标注视频、切片和归档，不承载运行态 MediaMTX 视频，也不承载源视频上传。

## 不采用的方案

- **让浏览器直连 `annotation-backend`**：需要公开服务端口或额外跨域鉴权，会形成绕过中心网关的路径。
- **在中心 API 中转发标注派生媒体读取**：把派生媒体读取耦合进产品控制面，并扩大中心故障对标注 UI 的影响。该否决不适用于 ADR-0012 的低频训练源视频上传。
- **本票新增派生媒体对象存储下载契约**：需要新的制品生命周期、签名下载和清理链路，超出本票；未来若媒体吞吐成为瓶颈再单独决策。

## 后果

Nginx 会为已授权的标注派生媒体承担一次低频字节转发，但不会改变运行态/证据媒体留在推理机的边界。源训练视频通过普通控制面上传入口流式进入 `dataset` 本地持久卷，不经过本 ADR 的 8444 派生媒体入口。`docs/deployment/nginx-annotation.conf.example`、[仓库架构](../engineering/architecture.md)、[ADR-0012](0012-media-ownership-and-center-training-files.md) 和控制面机制文档共同约束这些不同媒体路径，避免把任一例外误读为全局视频中继许可。

真实 HTTPS、Nginx、基座服务和浏览器行为仍由后置验证票 #82 负责；静态接线和中心授权由 Issue #31 的配置、用例和契约测试负责。
