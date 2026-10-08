# 标注组合证据运行说明

这组命令验证 NVIDIA 标注服务、中心授权和浏览器的真实部署边界。它保留 #31 的验收追溯；替身或 skip 不作为发布通过证据。

## 标注镜像冻结与验证

标注后端保持独立的 Python 3.10 环境，不并入 Center。基座 requirements 是直接依赖输入，部署约束在同一次解析中限定 NumPy ABI；完整传递依赖版本保存在部署拥有的锁文件中。

```sh
make annotation-lock        # 输入发生变化时显式重新生成
make annotation-lock-check  # 只核对，不改写锁；也由 make lockfile 执行
make annotation-image       # 干净构建、安装集合/兼容性、启动、合成视频上传/切片/下载
```

Docker 桥接构建网络无法访问包源时，使用仓库已有的显式选项 `make annotation-image NVSOP_DEV_BUILD_NETWORK=host`；不自动重试或修改依赖，运行容器仍在一次性隔离网络内。验证只使用合成媒体和临时数据库，沿用基座自己的 schema；它不替代 #224 的安装角色及完整训练对象验证，也不代表生产网关/浏览器验收。

该锁固定运行依赖版本，不额外列出发行文件哈希，因此不提供逐包下载内容哈希校验。基础镜像仍按 Dockerfile 的不可变 digest 固定；基础镜像中的安装工具随该 digest 固定，安装禁用隐式依赖求解和隔离构建依赖下载。OS 包索引与 apt 安装版本仍未冻结，不能宣称全镜像字节级可复现。完整 SBOM、漏洞与许可证证据仍由 #267 承担，离线制品闭包由 #266 承担。

本地构建/运行日志与实际镜像身份、平台及已安装包集合写入 `.nvsop/artifacts/annotation-*`。当前命令验证 `linux/amd64`；CI 仅在标注构建输入、验证工具或全量门禁入口受影响时运行镜像验证，文档快线不启动镜像。

## 统一入口部署

Nginx 与 NVIDIA 基座服务必须加入同一个内部 `sop-network`。只使用
`docs/deployment/nginx-annotation.conf.example`，将其第一组上游连接到
`control-api:8000`、`annotation-frontend:80` 和 `annotation-backend:8100`；标注 UI 和派生媒体客户端只访问
HTTPS 网关的 443/8444 端口。源训练视频经中心正式 API 流式写入 `dataset` 本地持久卷（ADR-0012 / #350），本页的集成命令不再需要 MinIO。标注后端和前端不发布 `ports`，不能直接从
厂区网络访问。训练服务的当前统一入口与配置见[运行配置](configuration.md)及实际 Nginx/Compose 资产，不再把 #33 当作未实施的部署步骤。标注 API 仅经中心适配器开放带产品上下文的调用，避免旧接口绕过数据集授权。

标注页面由中心训练数据集入口创建并准备上下文后，打开
`/annotation/?context=<opaque-context-token>`。React 页面只读取该已签发上下文和媒体 URL，
提交仍经过 `/api/annotation/` 兼容适配器；上传、清空和训练等基座旧写入口继续由中心拒绝。
标注派生视频、切片和归档通过 8444 媒体入口读取，这是 ADR-0011 记录的唯一视频中继例外；
运行态视频、源视频上传和中心控制面都不经该入口转发媒体字节。

部署前先用真实 Compose 渲染配置并确认训练服务没有宿主机端口：

```sh
docker compose -f vendor/sop-monitoring-blueprints/microservices/sop-training-bp/docker-compose.yml config
```

确认其中 `annotation-backend` 和 `annotation-frontend` 没有宿主机端口映射。若使用基座的独立
`microservices/video-annotator-ms/docker-compose.yml`，也必须保持同样的标注服务内部网络约束，
不得自行添加端口映射。

## 真实 NVIDIA 基座 + ARQ worker

准备一个可访问的 NVIDIA `video-annotator-ms` 实例、其 metadata PostgreSQL、真实 Redis，以及**隔离的本地训练素材卷**（`SOP_DATASET_STORAGE_ROOT`）后运行：

```sh
export NVSOP_ANNOTATION_BACKEND_URL=http://annotation-backend:8100
export NVSOP_ANNOTATION_MEDIA_ORIGIN=https://sop.example.internal:8444
cd apps/control-api
PYTHONPATH=src uv run pytest tests/integration/test_dataset_annotation_real.py -q -rs
```

`test_real_nvidia_annotation_backend_and_arq_worker_complete_a_submission` 不注入 `FakeAnnotationBackend`，而是通过中心 HTTP 路由创建数据集、上传并确认视频，再把真实 Redis outbox 交给 `arq.Worker`，由产品 runtime 使用 `HttpAnnotationBackend` 完成基座上传、下载和切片。

未提供上述部署变量时，测试明确 skip；skip 不是绿色验收证据。

## 真实 Nginx/HTTPS + 浏览器

只允许在一次性测试部署中运行，因为场景会创建数据集和视频：

```sh
export NVSOP_SYS31_ALLOW_MUTATION=1
export NVSOP_SYS31_GATEWAY_URL=https://sop.example.internal
export NVSOP_SYS31_LOGIN_NAME=...
export NVSOP_SYS31_PASSWORD_FILE=/run/secrets/sys31-password
export NVSOP_SYS31_CA_FILE=/run/secrets/sys31-ca.pem
export NVSOP_SYS31_E2E_URL="$NVSOP_SYS31_GATEWAY_URL"
export NVSOP_SYS31_E2E_LOGIN_NAME="$NVSOP_SYS31_LOGIN_NAME"
export NVSOP_SYS31_E2E_PASSWORD_FILE="$NVSOP_SYS31_PASSWORD_FILE"

pytest tests/system/test_sys_31_deployed.py -q
cd apps/control-web
env -u NODE_ENV pnpm exec playwright test tests/e2e/sys-31-annotation.spec.ts
```

系统测试覆盖正式网关登录、数据集/视频登记、准备状态、未拥有数据集拒绝、媒体全量读取、Range、注销后以统一 problem+json 重新拒绝和未开放兼容写操作。浏览器测试从真实部署进入训练数据集页面，点击真实 NVIDIA React 控件并等待中心返回已完成切片。

凭据文件、客户媒体和部署数据不得写入 Git。没有真实部署时，只能报告证据未证实，不能将 skip 解释为 SYS-31 通过。
