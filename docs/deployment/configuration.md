# 部署与运行配置

本文说明 NVSOP 当前配置入口和信任边界。代码是字段/校验的最终权威：中心进程配置见 `apps/control-api/src/factory_sop/settings.py`，中心下发配置见 `apps/control-api/src/factory_sop/configuration/`，边缘见 `apps/edge-runtime/src/edge_runtime/configuration.py`、`configuration_sync.py` 与 `runtime_configuration.py`，Web 开发代理见 `apps/control-web/vite.config.ts`。

## 中心后台

生产进程入口：

```sh
uvicorn --factory factory_sop.entrypoint:build
```

各进程入口/启动适配层负责从 `os.environ` 构造 `Settings`（包括 API、worker、bootstrap/healthcheck 等入口）；业务模块和用例层通过参数接收已经解析的配置，不直接读取进程环境。

### 配置规则

- 环境变量前缀固定为 `SOP_`。
- secret 不允许直接放在环境变量值中；必须使用对应的 `*_FILE` 变量，让值指向只读 secret 文件。
- 未识别的 `SOP_*` 变量会拒绝启动，防止拼写错误被静默忽略。
- 缺失、不可读、空 secret 或无效组合都会 fail-fast；没有“弱默认值继续运行”的降级路径。
- MinIO 对象存储和 Redis 是当前可运行中心实例的必需基础设施。

主要配置组：

| 组 | 典型变量 | 约束 |
|---|---|---|
| 日志 | `SOP_LOG_LEVEL` | `debug/info/warning/error` |
| PostgreSQL | `SOP_DATABASE_HOST/PORT/NAME/USER`, `SOP_DATABASE_PASSWORD_FILE` | 密码只能来自文件 |
| Session/CSRF | `SOP_SESSION_*`, `SOP_CSRF_SECRET_FILE` | absolute lifetime 不得短于 idle timeout |
| MinIO | `SOP_MINIO_ENDPOINT`, `SOP_MINIO_PUBLIC_ENDPOINT`, `SOP_MINIO_BUCKET`, access/secret `*_FILE` | endpoint/bucket/credentials 成组配置 |
| Redis | `SOP_REDIS_URL_FILE` | URL 必须是带主机的 `redis://` 或 `rediss://` |
| Dataset | upload TTL、max bytes、supported codecs | codec 列表不能为空 |
| Media probe | binary、timeout | 默认开发镜像使用 `ffprobe` |
| Annotation | backend URL、media origin、data root、timeouts | backend + media origin 成组，data root 为绝对路径 |
| Worker | health-check interval | 由 worker 运行环境提供 |

开发 Compose 默认使用 `SOP_DEPLOYMENT_MODE=fixed_main` + `SOP_SESSION_COOKIE_TRANSPORT=allow_http`，MinIO/annotation 本地入口分别是 `http://localhost:9443` 与 `http://localhost:8444`。`allow_http` 仍只允许与 `fixed_main` 联用；生产部署使用 HTTPS 安全边界。

开发 Compose 中的完整当前变量集合见 [`../../deploy/dev/compose.yaml`](../../deploy/dev/compose.yaml)，不要把其中的开发值复制成生产默认值。

## Web 开发代理

Vite 开发服务器把 `/api` 代理到：

```text
SOP_BACKEND_ORIGIN
```

未设置时默认为 `http://127.0.0.1:8000`。正式构建由 Nginx 同源提供 Web 资源和 `/api/v1`，不依赖 Vite 代理。

## 边缘运行时

生产入口：

```sh
NVSOP_EDGE_COMMAND_CONFIG_FILE=/etc/nvsop/edge.json \
  python -m edge_runtime
```

环境变量缺失会直接退出。配置文件为本机 JSON；当前中心 URL 只接受 **HTTPS**。

### 顶层字段

完整自治运行至少需要：

- `center_url`
- `host_id`
- `host_private_key_file`
- `command_timeout_seconds`
- `command_poll_interval_seconds`
- `connectors`
- `local_state_path`
- `stations`（非空）

可选：`center_ca_file`、`media`。

主机私钥从文件读取并校验；连接器凭据同样留在本机 secret 文件。**当前生产入口以这份本地 JSON 作为 bootstrap/本机部署配置**。设计上的权威分工是中心拥有拓扑、模板、版本和期望运行参数，本机文件拥有本机连接信息、adapter profile 和设备秘密；不要把本机 secret 反向写入中心配置或 Git。

### 工位配置形状

下面是字段形状示例；真实 URL、模板和 secret 路径由部署生成/确认：

```json
{
  "center_url": "https://center.example.internal",
  "host_id": "host-01",
  "host_private_key_file": "/run/secrets/host-private-key.pem",
  "center_ca_file": "/etc/nvsop/center-ca.pem",
  "command_timeout_seconds": 10,
  "command_poll_interval_seconds": 2,
  "connectors": [],
  "local_state_path": "/var/lib/nvsop/edge.sqlite3",
  "stations": [
    {
      "station_id": "station-01",
      "inference_url": "http://127.0.0.1:9000/v1/chat/completions",
      "request": {
        "stream": true,
        "model": "deployed-model",
        "messages": []
      },
      "template": {
        "steps": ["1", "2", "3"],
        "ordering": "ordered",
        "start_signal": "1",
        "end_signals": ["3"]
      },
      "parameters": {
        "idle_timeout": 30,
        "step_deadline": 10
      },
      "margins": {
        "leading": 2,
        "trailing": 2
      }
    }
  ]
}
```

运行时要求 station request 的 `stream` 为 `true`；station IDs 必须唯一。推理 URL可为 HTTP/HTTPS，因为它通常是推理机本机链路；`center_url` 仍必须 HTTPS。

### 连接器

当前本机配置解析器支持的生产 connector type 是 `hikvision_isapi`。每个连接器配置包含 ID、revision、`credentials_configured`、`base_url`、ISAPI profile 和能力声明。若 `credentials_configured=true`，必须提供 `username_file` 与 `password_file`；秘密文件不得为空。

能力声明是现场实测事实，不是从型号名猜出的能力。模板绑定和判定依赖能力数据；详见 [`../design/mechanisms/edge-autonomy.md`](../design/mechanisms/edge-autonomy.md)。

## 当前配置变更与同步机制

`python -m edge_runtime` 仍先读取 `NVSOP_EDGE_COMMAND_CONFIG_FILE` 指向的本地 JSON。这份文件是**已实现的 bootstrap 与主机本地配置入口**：它提供中心地址、主机身份/私钥、本地推理端点与请求体、adapter profile、设备凭据、本地 SQLite 路径，以及首次无法取得中心确认配置时的自治起点；它不是中心拥有的拓扑、模板和运行参数的第二份权威。

自治运行时启动后会立即通过带主机签名的 `GET /api/v1/inference-hosts/{host_id}/configuration` 拉取当前主机的配置 bundle。中心先认证主机身份，再只组装该主机的有效后端、工位、相机、连接器/点位、模板版本和运行参数。共享配置契约校验 contract version、revision 和 canonical SHA-256；Edge 拉取层另行校验返回 bundle 的 `host_id` 必须等于本机身份，并证明 bundle 能与本机保存的推理端点、请求体、adapter profile 和凭据安全组合，再允许它替换已确认视图。

统一 Nginx 入口只对当前主机签名机器路径做显式白名单分流：主机配置拉取与已确认配置历史握手、monitor decision/health 上报、delegated command 领取/结果回报，以及模板配置确认上报。这些路径不经过浏览器 session `auth_request`，也不使用浏览器 Cookie、Authorization 或 CSRF 身份；`X-Inference-Host-ID`、timestamp、nonce、signature 则保持原请求值并由 FastAPI 的主机签名认证最终校验。其余 `/api/v1/` 管理接口仍由现有 session + CSRF + permission 边界保护，annotation 内部授权入口和媒体网关不在该白名单内。

确认由 `LocalConfigurationStore` 在 SQLite 的单个事务中写入完整 bundle：跨主机、旧 revision、同 revision 不同内容或无效运行组合都不会覆盖现有确认值。拉取、解析或运行组合验证失败时，只更新 `local_config_failure` 诊断，最后已确认 bundle 保持不变。启动时已有确认值就优先使用它；首次启动尚无确认值且中心不可达时，才使用本地 bootstrap 配置。

运行期间 maintenance loop 按 `command_poll_interval_seconds` 继续拉取。新的有效 bundle 与当前 effective digest 不同时，当前运行循环先停止，再从已确认 bundle 重新组合 station/connector/runtime；中心不可达不会把配置同步放进实时判定进度。

因此配置变更按所有权分两条路径：中心拥有的拓扑、模板、版本、点位和运行参数通过中心数据与配置同步生效；本机推理地址、请求体、adapter profile、设备秘密等主机本地信息通过受管本地 JSON/secret 文件变更，并按部署流程重启或重新装配。不要直接修改 SQLite 来制造确认状态。
