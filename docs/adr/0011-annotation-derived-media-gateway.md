# 标注派生媒体经中心授权的 Nginx 入口

## 状态

已采纳，适用于 Issue #31 的标注 UI。

## 背景

系统的运行态视频和预览视频必须由浏览器直连推理机 MediaMTX，不能经过中心入口或 Nginx 中继。训练视频上传也走 MinIO 预签名地址，不能让浏览器把源视频上传到中心。

复用的 NVIDIA React 标注 UI 还需要在浏览器中读取已准备的标注视频、切片和归档。`annotation-backend` 只加入内部 Compose 网络，不能给浏览器一个绕过网关的服务端口；让浏览器直连它会破坏中心会话和数据集权限边界。

## 决策

保留一个明确、有限的例外：标注准备产生的**派生媒体**经 8444 Nginx 媒体入口读取。该入口必须同时满足：

- 先向中心执行 `auth_request`，中心按会话、数据集权限、上下文或提交归属核对资源；
- Nginx 只把中心返回的 `X-Annotation-Upstream-Video-ID` 或 `X-Annotation-Upstream-Clip-ID` 转成内部基座请求，不接受客户端提供的上游 URL 或路径；
- `annotation-backend` 不发布宿主机端口，Nginx 清空 Cookie、Authorization 和其他中心身份头；
- 入口只包含标注视频、切片和归档，不承载运行态 MediaMTX 视频，也不承载源视频上传。

## 不采用的方案

- **让浏览器直连 `annotation-backend`**：需要公开服务端口或额外跨域鉴权，会形成绕过中心网关的路径。
- **在中心 API 中转发媒体**：把低频派生媒体传输耦合进产品控制面，并扩大中心故障对标注 UI 的影响。
- **本票新增派生媒体对象存储下载契约**：需要新的制品生命周期、签名下载和清理链路，超出本票；未来若媒体吞吐成为瓶颈再单独决策。

## 后果

Nginx 会为已授权的标注派生媒体承担一次低频字节转发，但不会改变运行态视频的直连边界，也不会让源视频上传经过网关。`docs/deployment/nginx-annotation.conf.example`、[`repository-architecture.md`](../design/repository-architecture.md) 和控制面机制文档共同引用本 ADR，避免把该例外误读为全局视频中继许可。

真实 HTTPS、Nginx、基座服务和浏览器行为仍由后置验证票 #82 负责；静态接线和中心授权由 Issue #31 的配置、用例和契约测试负责。
