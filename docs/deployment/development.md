# 本地开发环境

本文是 NVSOP 固定 `main` 开发实例的安装、启动和日常操作入口。脚本权威是 [`scripts/dev.py`](../../scripts/dev.py)，稳定命令入口是根 [`Makefile`](../../Makefile)。任务代码仍应在独立 `agent/...` 分支/worktree 修改；**不要为了运行开发实例而直接在 `main` 上做任务修改**。

## 运行模型

固定开发实例名为 `nvsop-dev-main`。`scripts/dev.py` 只允许从**主工作树的本地 `main` 分支**启动，并按 `main` 的提交制作运行快照；任务 worktree 不能直接启动这套实例。

推荐工作流：

1. 从已接受的 `main` 创建 `agent/<agent-id>/<task-slug>` worktree 开发和验证文档/代码。
2. 经仓库集成流程把已接受变更推进到主线。
3. 在主工作树的 `main` 上运行/刷新固定开发实例验证集成结果。

## 前置工具

当前工具链版本直接读取仓库的机器可读来源，本文不复制版本值：

- Python：根 [`.python-version`](../../.python-version)。
- Node：根 [`.nvmrc`](../../.nvmrc)。
- pnpm：根 [`package.json`](../../package.json) 的 `packageManager`。
- Tilt：[`scripts/dev.py`](../../scripts/dev.py) 的 `TILT_VERSION`。

还需要 `git`、`git-lfs`、Docker + Docker Compose、`ffmpeg`、`ffprobe`、`uv`。只有显式 HTTPS 模式需要 `openssl`。`make dev-setup` 默认也会校验 Node 和 Playwright UI 固定端口 9323；Docker daemon 必须可用。

固定端口必须空闲：

| 服务 | 地址/端口 |
|---|---|
| 业务入口 | `http://localhost:8443`（默认） |
| 标注派生媒体 | `http://localhost:8444` |
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
- 拉取 PostgreSQL、Redis、Nginx 等基础镜像；训练素材使用本地 named volume，不需要对象存储镜像；
- 在 `.nvsop/dev-main/`（或 `--state-dir` 指定目录）写入状态、凭据路径和测试制品。

开发账号信息写入 `.nvsop/dev-main/credentials.txt`，默认登录名为 `dev.admin`；密码本身保存在独立 secret 文件中。不要把状态目录、密码或证书私钥提交进 Git。

开发实例只运行一套 PostgreSQL（`center-db`），其中有两个独立 database：Center 使用 `nvsop`，原样复用的训练/标注进程使用 `training`（Q35）。`training` database 由 `training-db-init` 幂等创建，仅保证空库存在；五类训练/标注进程所需对象由 `training-objects-install` 在角色初始化之后用安装身份 `nvsop` 安装（S066）：它复用 Vendor 合并 DDL（`db-init-scripts/01-init-tables.sql`），并把消费的 Vendor 提交与 DDL 摘要记录进 `nvsop_training_install`。空库首次安装；受支持的已知 Vendor 标注来源（原始 standalone 四表，或同形但带 `dataset.two_operator_mode` 的四表）按实际结构识别后增量升级：只从锁定的 Vendor 合并 DDL 补齐缺失对象、按需补 `two_operator_mode` 列，不重跑空库整体初始化、也不在本仓库复制 DDL；重复启动跳过；未知非空、部分训练对象或结构不符的来源明确拒绝，不删除或覆盖既有数据。安装/升级版本（含来源形态 `source_version`）记录在 `nvsop_training_install`。受支持历史数据的迁移走手工 `pg_dump`/`pg_restore` 恢复到空 `training` 的流程（restore → `training-role-init` → `training-objects-install`），见[升级与兼容性](upgrade.md#training-database-增量升级)。`center-role-init` 与 `training-role-init` 用安装身份 `nvsop` 在启动时分别收敛 `nvsop` 与 `training` 的角色权限（S065）：Center 迁移/安装用超级用户 `nvsop`，长期运行的中心服务用非超级用户 `nvsop_runtime`，训练/标注进程用非超级用户 `training_runtime`；两个 runtime 只在各自 database 内拥有 `CONNECT`、schema `USAGE`、表/序列/类型/函数权限与未来对象 default privileges，`PUBLIC` 既不能旁路 `CONNECT` 也不能旁路表/序列对象权限（函数 `EXECUTE` 与类型 `USAGE` 保留 PostgreSQL 对 PUBLIC 的内建授权：`ALTER DEFAULT PRIVILEGES ... REVOKE ... FROM PUBLIC` 无法撤销这两类内建默认，实测撤销后新建函数仍带 `=X/owner`；runtime 改用显式授权，CONNECT 边界已阻止其他角色进入）。角色权限隔离以完整栈为准：Center 服务依赖 `center-role-init`，训练服务依赖 `training-objects-install`（其自身依赖 `training-role-init`）；标注后端要等对象安装成功才启动，安装失败会显式停止而不是让请求打到缺失的表。runtime 密码保存在状态目录的独立 secret 文件（`nvsop-runtime-password`、`training-runtime-password`）。既有开发实例升级后需先重新运行 `make dev-setup` 生成这两个 secret，否则启动/运行路径（`make dev`/`dev-smoke`/`dev-test-ui`）会因 `require_setup` 缺少文件被拒；`make dev-down`/`dev-logs` 不依赖它们。原 `annotation-db` 服务及其 Compose 卷声明已从 `deploy/dev/compose.yaml` 删除，该卷 `nvsop-dev-main-annotation-db` 因此不再出现在 Compose 的卷清单中；但删除声明**不会**删除宿主机上既有的 named volume：`make dev-down` 从不删除卷，只有显式 `docker volume rm`（或 `docker compose down -v`）才会。该卷的数据迁移按[升级与兼容性](upgrade.md#training-database-增量升级)的手工备份/恢复流程处理，本开发实例不自动迁移。旧工作树若仍有 `.tmp/dev-main/`，先停止旧实例；需要保留本地状态时显式移动到 `.nvsop/dev-main/`，否则重新执行 `make dev-setup`。脚本不会静默回退到旧目录。

中心 `center-db` 服务端镜像自带 TimescaleDB（S020），扩展由迁移中的 `CREATE EXTENSION` 安装；`training` 仍是普通 PostgreSQL 库，用 `TEMPLATE template0` 建库以避免继承镜像装入 template1 的扩展。镜像切换与既有数据卷的升级步骤见 [`upgrade.md`](upgrade.md)。

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

`make dev-down` **不会删除 named volumes**；输出中的 `volumes_removed` 保持 `false`。数据库、Redis、annotation media 与 dataset 训练素材卷的开发数据因此会保留。

## 常见排障

### 当前分支不是 `main`

`dev.py` 会拒绝启动。不要在任务分支上绕过检查；切回主工作树的 `main`，任务修改继续留在独立 worktree。

### 固定端口被占用

先结束占用 8443/8444/10350/9323 的旧实例或进程，不要修改端口规避共享开发实例约定。

### Docker build 无法访问包镜像

仅在 Docker bridge 的构建网络无法访问包镜像时可临时使用：

```sh
NVSOP_DEV_BUILD_NETWORK=host make dev
```

它只改变镜像**构建**网络；运行时仍使用 Compose 网络。


## 开发实例不是发布证明

`make dev-smoke`、浏览器测试和固定 Compose 实例证明本地软件链路，不替代 [`limitations.md`](limitations.md) 与 [`../research/target-environment-validation-matrix.md`](../research/target-environment-validation-matrix.md) 中的 GPU、相机、连接器、离线安装、容量和长稳门禁。
