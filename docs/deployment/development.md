# 本地开发环境

本文是 NVSOP 固定 `main` 开发实例的安装、启动和日常操作入口。脚本权威是 [`scripts/dev.py`](../../scripts/dev.py)，稳定命令入口是根 [`Makefile`](../../Makefile)。任务代码仍应在独立 `agent/...` 分支/worktree 修改；**不要为了运行开发实例而直接在 `main` 上做任务修改**。

## 运行模型

固定开发实例名为 `nvsop-dev-main`。`scripts/dev.py` 只允许从**主工作树的本地 `main` 分支**启动，并按 `main` 的提交制作运行快照；任务 worktree 不能直接启动这套实例。

推荐工作流：

1. 从已接受的 `main` 创建 `agent/<agent-id>/<task-slug>` worktree 开发和验证文档/代码。
2. 经仓库集成流程把已接受变更推进到主线。
3. 在主工作树的 `main` 上运行/刷新固定开发实例验证集成结果。

## 前置工具

当前工具链权威版本来自仓库文件：

- Python：`.python-version`，当前为 **3.12**。
- Node：`.nvmrc`，当前为 **22.23.2**。
- pnpm：根 `package.json#packageManager`，当前为 **11.22.0**。
- Tilt：开发编排器强制 **0.37.7**。

还需要 `git`、`git-lfs`、Docker + Docker Compose、`ffmpeg`、`ffprobe`、`uv`。只有显式 HTTPS 模式需要 `openssl`。`make dev-setup` 默认也会校验 Node 和 Playwright UI 固定端口 9323；Docker daemon 必须可用。

固定端口必须空闲：

| 服务 | 地址/端口 |
|---|---|
| 业务入口 | `http://localhost:8443`（默认） |
| 标注派生媒体 | `http://localhost:8444` |
| MinIO 上传入口 | `http://localhost:9443` |
| Tilt | `http://localhost:10350` |
| Playwright UI | `http://localhost:9323` |

固定 `main` 本地实例默认使用 HTTP。需要验证本地 TLS 时显式设置 `NVSOP_DEV_PROTOCOL=https`；正式部署仍使用 HTTPS 安全边界。

## 第一次准备

在**主工作树的 `main`**：

```sh
make hooks
make dev-setup
```

`make dev-setup` 会：

- 校验工具、Docker daemon、Compose、Tilt 版本和固定端口；
- 生成开发 secrets 和合成测试视频；显式 HTTPS 模式才生成开发 CA/服务端证书；
- 执行 frozen `pnpm install` 与 `uv sync`；
- 拉取 PostgreSQL、Redis、MinIO、annotation DB、Nginx 等基础镜像；
- 在 `.tmp/dev-main/`（或 `--state-dir` 指定目录）写入状态、凭据路径和测试制品。

开发账号信息写入 `.tmp/dev-main/credentials.txt`，默认登录名为 `dev.admin`；密码本身保存在独立 secret 文件中。不要把状态目录、密码或证书私钥提交进 Git。

### 可选 HTTPS CA

默认 HTTP 不生成也不要求本地 CA。需要本地 TLS 时重新准备并以同一协议启动：

```sh
NVSOP_DEV_PROTOCOL=https make dev-setup
NVSOP_DEV_PROTOCOL=https make dev
```

HTTPS 模式会在状态目录的 `tls/` 生成开发 CA，并生成 `TRUST-CA.txt`。浏览器访问前，把 `ca.crt` 导入当前用户的可信根证书存储。Playwright 和本地 HTTPS 客户端使用同一 CA，不以“跳过 TLS 校验”作为正常工作流。

## 日常命令

`make dev` 是前台常驻 launcher：

```sh
make dev             # 启动/保持固定开发实例；当前终端持续占用直到停止
```

在另一个终端执行状态、日志、刷新和测试命令：

```sh
make dev-status      # 输出实例、服务、liveness/readiness 状态
make dev-logs        # Compose 日志，默认 tail 200
make dev-refresh     # 切到 main 的新提交快照
make dev-smoke       # 真实入口功能冒烟
make dev-test-ui     # Playwright UI，2 workers
make dev-down        # 停止实例
```

日志可限定服务和行数：

```sh
make dev-logs SERVICE=worker TAIL=200
```

`make dev-down` **不会删除 named volumes**；输出中的 `volumes_removed` 保持 `false`。数据库、Redis、MinIO 和 annotation media 的开发数据因此会保留。

## 常见排障

### 当前分支不是 `main`

`dev.py` 会拒绝启动。不要在任务分支上绕过检查；切回主工作树的 `main`，任务修改继续留在独立 worktree。

### 固定端口被占用

先结束占用 8443/8444/9443/10350/9323 的旧实例或进程，不要修改端口规避共享开发实例约定。

### Docker build 无法访问包镜像

仅在 Docker bridge 的构建网络无法访问包镜像时可临时使用：

```sh
NVSOP_DEV_BUILD_NETWORK=host make dev
```

它只改变镜像**构建**网络；运行时仍使用 Compose 网络。


## 开发实例不是发布证明

`make dev-smoke`、浏览器测试和固定 Compose 实例证明本地软件链路，不替代 [`limitations.md`](limitations.md) 与 [`../research/target-environment-validation-matrix.md`](../research/target-environment-validation-matrix.md) 中的 GPU、相机、连接器、离线安装、容量和长稳门禁。
