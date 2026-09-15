# 部署与运行配置

本文说明 NVSOP 当前配置入口和信任边界。代码是字段/校验的最终权威：中心见 `apps/control-api/src/factory_sop/settings.py`，边缘见 `apps/edge-runtime/src/edge_runtime/configuration.py` 与 `station_runtime.py`，Web 开发代理见 `apps/control-web/vite.config.ts`。

## 中心后台

生产进程入口：

```sh
uvicorn --factory factory_sop.entrypoint:build
```

只有该进程入口读取 `os.environ`；下层应用/用例通过参数接收已经解析的配置。

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

`SOP_SESSION_COOKIE_TRANSPORT=allow_http` 只允许与 `SOP_DEPLOYMENT_MODE=fixed_main` 联用，并且 MinIO/annotation 本地入口必须分别是 `http://localhost:9443` 与 `http://localhost:8444`。生产部署使用 HTTPS 安全边界。

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

主机私钥从文件读取并校验；连接器凭据同样留在本机 secret 文件。中心只拥有拓扑、模板、版本和生效运行参数；本机文件拥有本机连接信息、adapter profile 和设备秘密。不要把本机 secret 反向写入中心配置或 Git。

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

## 配置变更与最后确认状态

边缘自治的目标是中心短时不可达时继续使用**最后已确认**的模板/配置和本地状态。新配置必须经过既定确认/切换语义后生效；不要通过手改 SQLite 或绕过配置入口制造“看似已更新”的状态。
