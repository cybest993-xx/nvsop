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
- Redis 是当前可运行中心实例的必需基础设施。训练素材由 `dataset` 拥有的中心本地持久卷保存（`SOP_DATASET_STORAGE_ROOT`），不需要独立对象存储服务（ADR-0012）。
- 训练视频经业务网关流式上传：入口 `client_max_body_size` 必须不小于 `SOP_DATASET_MAX_UPLOAD_BYTES`（开发网关对上传路径为 8192M，对应默认 8 GiB），且上传路径不得由网关先缓冲整个请求体。

主要配置组：

| 组 | 典型变量 | 约束 |
|---|---|---|
| 日志 | `SOP_LOG_LEVEL` | `debug/info/warning/error` |
| PostgreSQL | `SOP_DATABASE_HOST/PORT/NAME/USER`, `SOP_DATABASE_PASSWORD_FILE` | 密码只能来自文件 |
| SSE 订阅 | `SOP_MONITOR_MAX_SUBSCRIPTIONS` | 每个 API 进程的独立监听连接预算；默认值与上限由 Settings 拥有，超限返回 503 |
| Session/CSRF | `SOP_SESSION_*`, `SOP_CSRF_SECRET_FILE` | absolute lifetime 不得短于 idle timeout |
| 训练素材 | `SOP_DATASET_STORAGE_ROOT` | 必须是中心机上的绝对路径；写入经正式 API 流式完成 |
| VLM 训练 | `SOP_VLM_TRAINING_BACKEND_URL`, `SOP_VLM_TRAINING_DATASET_ROOT`, `SOP_VLM_TRAINING_RESULTS_ROOT`, `SOP_VLM_TRAINING_HTTP_TIMEOUT_SECONDS` | URL/两个绝对根目录须成组；中心不读基座训练配置（读取器契约由代码中的冻结常量约束），未配置 URL 时任务保持 held |
| DDM 训练 | `SOP_DDM_TRAINING_BACKEND_URL`, `SOP_DDM_TRAINING_DATASET_ROOT`, `SOP_DDM_TRAINING_RESULTS_ROOT`, `SOP_DDM_TRAINING_CONFIG_PATH`, `SOP_DDM_TRAINING_HTTP_TIMEOUT_SECONDS` | URL/两个绝对根目录须成组；配置路径绑定基座实际训练配置，HTTP timeout 只约束查询/派发，不是训练时限。未配置 URL 或配置路径时任务保持 held |
| Redis | `SOP_REDIS_URL_FILE` | URL 必须是带主机的 `redis://` 或 `rediss://`；库号只能写在路径中（`redis://host:port/N`），URL 的 `db` 查询参数不会被读取 |
| Dataset | upload TTL、max bytes、supported codecs | codec 列表不能为空 |
| Media probe | binary、timeout | 默认开发镜像使用 `ffprobe` |
| Annotation | backend URL、media origin、data root、timeouts | backend + media origin 成组，data root 为绝对路径 |
| Worker | health-check interval | 由 worker 运行环境提供 |

多 API 进程部署时，监听预算按进程累加；配置 PostgreSQL 总连接预算时须同时计入各进程的业务池与监听池，不能把单进程订阅上限当作集群总上限。

开发 Compose 默认使用 `SOP_DEPLOYMENT_MODE=fixed_main` + `SOP_SESSION_COOKIE_TRANSPORT=allow_http`。annotation 本地入口是 `http://localhost:8444`；训练视频经同一个业务网关 `http://localhost:8443` 流式上传，不再有独立对象存储上传入口。`allow_http` 仍只允许与 `fixed_main` 联用；生产部署使用 HTTPS 安全边界。

开发 Compose 中的完整当前变量集合见 [`../../deploy/dev/compose.yaml`](../../deploy/dev/compose.yaml)，不要把其中的开发值复制成生产默认值。

### DDM 共享路径与读取边界

DDM 的 `DATASET_ROOT`/`RESULTS_ROOT` 与 worker 的对应 SOP 根目录必须是**同一卷、同一容器内绝对路径**：基座把路径写入训练配置，结果核对不能用另一条映射路径猜测。worker 对输入卷可写、结果卷只读；DDM 对结果卷可写。`SOP_DDM_TRAINING_CONFIG_PATH` 指向与 DDM `CONFIG_PATH` + `TRAIN_CONFIG_NAME` 相同的受信部署文件，挂载只读。worker 在执行前才读实际 reader 字段并与冻结的每角色参数比较；缺文件/字段或不匹配产生持久诊断，不启动训练。配置文件读取和基座连通性不作为中心/worker 启动前提，DDM 离线不阻止登录、模板及其他控制 API。

VLM 基座的 `DATASET_ROOT`/`RESULTS_ROOT` 有同一条同卷同绝对路径约束：中心把待派发输入材料化到 `DATASET_ROOT/<vlm-命令身份>`，并按基座真实写出的合并配置核对它确实消费了这份输入；worker 对输入卷可写、结果卷只读，基座对结果卷可写。VLM 没有对应 `SOP_VLM_TRAINING_CONFIG_PATH`：基座自己的 `CONFIG_PATH`/`TOOL_PATH` 只影响基座，中心不依赖它们也能完成消费核对与结果登记。

已经登记的结果保存在 `SOP_DATASET_STORAGE_ROOT`；Nginx 的 8444 数据服务只读挂载此卷，经当前会话与数据集查看权限授权后读取精确登记对象，控制 API 不传结果字节。对象键命名空间有两个：DDM 的 `ddm-results/{命令}/配置|checkpoint` 与 VLM 的 `vlm-results/{命令}/配置|checkpoint 清单`（后者不复制权重字节）。规则见 [ADR-0011](../adr/0011-annotation-derived-media-gateway.md)，配置见 [Nginx 示例](nginx-annotation.conf.example)；本地 Compose 接线见[开发部署](development.md#ddm-训练的可选部署)。

### 证据访问授权

`SOP_EVIDENCE_HOST_ORIGINS` 是中心部署配置的 JSON 对象：键为已登记来源推理机的规范 UUID，值为浏览器可达的证据服务 HTTP(S) origin（不得含路径、凭据、查询或片段）。缺省为空；缺少该主机映射时签发返回 `source_host_unconfigured`，不能从浏览器参数、主机地址或猜测端口补齐。HTTP 只可用于 `fixed_main` + `allow_http` 本地部署。开发 Compose 的同一变量既送入中心进程环境，也由 `scripts/dev.py` 在渲染 `deploy/dev/nginx.conf` 时写入 `connect-src`/`media-src`，使浏览器能直连已配置来源；未配置来源仍被 CSP 拒绝。畸形 JSON、非规范 UUID、非法 origin 或注入字符在渲染阶段 fail-fast，不做弱默认降级。

用户须有 `evidence.evidence.view` 权限。中心只签发绑定具体证据 ID、来源主机、会话指纹和短有效期的上下文，不返回媒体字节；来源主机每次读取均通过签名 `POST /api/v1/evidence/access-check` 复核当前会话与权限。登出、会话过期和撤权使旧上下文无法继续读取，上下文不延长会话。

业务网关仅将 `/api/v1/evidence/registrations` 与 `/api/v1/evidence/access-check` 两条精确路径交给主机签名认证，清除 Cookie/Authorization/CSRF 而保留签名头；列表与签发仍走浏览器会话认证。中心不挂载、保存或代理证据目录。Edge 读取服务和 Web 播放入口另由各自部署步骤提供，只有授权协议不代表浏览器已可播放。

### 保留影响估算

缩短确认的有效期由中心环境 `SOP_RETENTION_ESTIMATE_TTL_SECONDS` 显式给出正整数秒，无业务默认值。缺失时中心仍可查看/延长，但拒绝缩短并返回 `RETENTION_ESTIMATE_TTL_UNCONFIGURED`；到期确认返回 `RETENTION_ESTIMATE_STALE`，需重新估算。不要借用命令租约作为有效期。

本地已上报记录统计还须设置 `SOP_RETENTION_IMPACT_TIMEOUT_SECONDS`（正的有限秒数），并为全部来源机在 `SOP_EVIDENCE_HOST_ORIGINS` 中登记中心可达的服务 origin。缺超时/有效期不发送统计请求，缺 origin、未确认配置、未知历史确认时刻或来源不可达使估算不完整，不能确认相关缩短；不轮询、不缓存、不重试，不接受部分汇总。

来源机按[独立证据服务](../../deploy/media/README.md#来源主机证据回看服务)增加 `--local-state <运行时实际使用的-SQLite-绝对路径>`，只读打开已由运行时迁移的状态；服务不创建、迁移或清理数据库。该机必须已经确认中心最新签发的配置。业务网关将精确 `POST /api/v1/retention/reported-impact/access-check` 与其他机器入口一样保留签名头、清除 Cookie/Authorization/CSRF；不能将整个 retention 路径豁免浏览器认证。票据只授权本次统计并逐次复核 `retention.policy.edit`，不授予媒体访问或删除权。

### 证据删除资格

删除票据有效期由中心环境 `SOP_EVIDENCE_DELETION_TICKET_TTL_SECONDS` 显式给出正整数秒，无业务默认值；缺失时 `claim` 整批拒绝并返回 `ticket_ttl_unconfigured`，`verify` 不产生新的删除授权（未授权认领同样返回 `ticket_ttl_unconfigured`），而已授权认领仍可复核以完成已有删除意图，`result` 也仍接受已持有认领的终局反馈以便完成已开头的清理。来源机在本机取消链接证据媒体前，经主机签名的 `POST /api/v1/retention/evidence-deletion-claims`（claim）、`.../verify`（删前复核并授权）与 `.../result`（终局反馈）领取/复核/结案一条删除认领。`verify` 放行即在同一权威锁下把认领推进到已授权阶段并持久：在来源机终局反馈前，修改保留策略、停用/恢复工位或签发绑定配置都会因权威锁被拒绝（`STATE_CONFLICT`），不能改写已放行的删除依据；未授权认领仍会因策略/配置变化返回既有 stale 原因。有效配置未变（`effective_sha256` 不变）的签名配置拉取与租约续期不推进修订，不被该权威锁阻断；只有真正改变删除所依据绑定权威的配置签发才被原子拒绝。未结案认领等价于该引用继续在审保护，不因票据过期自动释放，只有来源机的终局反馈才结案。来源机在取消链接与结果落账之间使用真实 SQLite 写事务，先落本机删除意图，上报失败或重启后重放，不从文件缺失推断成功。业务网关只放行这三个精确路径并保留签名头、清除 Cookie/Authorization/CSRF；不豁免整个 retention 路径，也不授予浏览器任何删除能力。中心只回答删除资格，实际取消链接只发生在拥有文件的推理机（ADR-0012）。

升级运行时会在 SQLite V20 给四类上报账本追加可空 `acknowledged_at`，以后只在中心成功响应后冻结首次确认 UTC。历史发送行不回填，不得手工用发送单调时刻或接收时刻伪造确认。估算返回逻辑序列化字节，不能据此承诺磁盘空间或删除媒体。稳定整链路入口见[真实 PG/HTTPS/SQLite 系统场景](../../tests/system/test_sys_42_retention_impact.py)。

### 回收执行调度

中心回收任务复用既有 worker 进程的 cron，不新增服务。调度周期由 `SOP_RETENTION_SWEEP_INTERVAL_SECONDS` 显式给出正整数秒；缺失时回收不运行，不提供业务默认值。每张候选表的单轮有界行数由 `SOP_RETENTION_SWEEP_BATCH_LIMIT` 配置（默认 256，上限 10000）。每个周期以确定性身份在 `retention_sweep` 账本登记一行：同周期并发只有一个任务执行，崩溃残留被同周期重跑覆盖，失败行不阻塞下一周期重跑。`monitor` 明细记录删除前把窗口事实按工位/实例/小时聚合并写入 `monitor_record_archive`；违规与处置归档默认永久，不因回收删除。来源机的证据媒体删除由 #206 的本机执行器周期推进（低频有界 drain），中心不删除媒体字节（ADR-0012）。

## Web 开发代理

Vite 开发服务器把 `/api` 代理到：

```text
SOP_BACKEND_ORIGIN
```

未设置时默认为 `http://127.0.0.1:8000`。正式构建由 Nginx 同源提供 Web 资源和 `/api/v1`，不依赖 Vite 代理。

## 边缘运行时

边缘运行时声明了少量第三方依赖（`apps/edge-runtime/pyproject.toml`），由根 `uv.lock` 冻结。推理机按锁文件安装，只装边缘包及其依赖：

```sh
uv sync --frozen --no-dev --package edge-runtime
```

离线主机需预先按同一锁文件准备 wheel，安装时不得联网下载（离线安装门禁见 [limitations.md](limitations.md)）。

生产入口：

```sh
NVSOP_EDGE_COMMAND_CONFIG_FILE=/etc/nvsop/edge.json \
  python -m edge_runtime
```

生产入口在装配前初始化标准错误日志输出，进程守护应采集 stderr，保留 `edge.connector.write_attempt` 点位诊断。默认级别与输出格式由 [`runtime.py`](../../apps/edge-runtime/src/edge_runtime/runtime.py) 的入口拥有；日志不输出连接器凭据或主机私钥，不另建审计存储。

环境变量缺失会直接退出。配置文件为本机 JSON；当前中心 URL 只接受 **HTTPS**，且必须是最终地址：边缘不跟随 3xx 跳转，因为请求签名绑定原始路径。

`python -m edge_runtime` 是主机进程，负责判定、处置、证据切片和**唯一的** MediaMTX 预览/录像。每台推理机的推理服务（DeepStream + DDM + vLLM）是独立容器层，按「主机 × 后端」一个 Compose project 部署；其入口、资源/端口分配与只读 secret 边界见[推理机推理服务部署](../../deploy/edge/README.md)，不要在该层另起第二个 MediaMTX 或第二套录像。

**必须由进程守护自动重启。** 工位或本地状态出现未预期异常时，运行时按快速失败退出，重启后从本机 SQLite 恢复（在飞实例以 `RUN_INTERRUPTED` 结案）；中心相关线程的异常不会导致退出（见 [edge-autonomy.md](../design/mechanisms/edge-autonomy.md)）。例如 systemd：

```ini
[Service]
Environment=NVSOP_EDGE_COMMAND_CONFIG_FILE=/etc/nvsop/edge.json
ExecStart=<边缘虚拟环境>/bin/python -m edge_runtime
Restart=always
RestartSec=2
```

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

可选：`center_ca_file`、`media`、`evidence`、`queue_capacity`、`disk_health`。

`queue_capacity` 可选，未填写时四类待办分别默认最多 `10000` 条。显式设置时必须完整指定正整数 `reports`、`evidence`、`observations`、`health`。这些限制仅约束本机尚未确认的 SQLite 行数，不代表磁盘字节配额；部署仍须监控本机磁盘水位。处置写入账本不属于可丢弃队列。

```json
"queue_capacity": {
  "reports": 10000,
  "evidence": 10000,
  "observations": 10000,
  "health": 10000
}
```

`LocalState.queue_status()` 在本机提供四类待办的深度、最早 `queue_id`、最近待办失败与容量边界；上报循环周期性记录 `edge.local_queue.status`，不输出可能含秘密的原始失败信息。达到容量时，新增待办拒绝并记录 `edge.local_queue.capacity_exhausted`，判定反应事务整体回滚；运行时另发 `edge.station.judgment_unavailable` 告警并按现有本地致命错误路径退出，由守护进程重启，不制造额外 `INDETERMINATE`/正常判定，不能对外声称已锁存或已完成判定。不得丢弃未确认的唯一副本，需修复积压并取得中心确认后恢复容量。磁盘自身写入失败同样不能被归类为单纯中心传输失败。

主机私钥从文件读取并校验；连接器凭据同样留在本机 secret 文件。**当前生产入口以这份本地 JSON 作为 bootstrap/本机部署配置**。设计上的权威分工是中心拥有拓扑、模板、版本和期望运行参数，本机文件拥有本机连接信息、adapter profile 和设备秘密；不要把本机 secret 反向写入中心配置或 Git。

### 磁盘水位与不可写告警

`disk_health` 对 SQLite 所在目录、参与该工位连续录像的目录和已配置的证据目录分别采样，不假设它们共用文件系统。它使用当前进程可用块统计，并在目标目录创建、写入、`fsync` 后移除自己的小型探测文件。显式配置必须完整，例如：

```json
"disk_health": {
  "high_watermark_percent": 90,
  "recovery_watermark_percent": 80,
  "check_interval_seconds": 5
}
```

使用率 **≥ 高水位**、采样失败或探测不可写时，该路径进入失效；只有探测成功且使用率 **≤ 恢复边界**才恢复，中间区间保持失效。任一路径失效都通过 `STORAGE_UNAVAILABLE` 使该工位不可判定；其他路径或推理流恢复不能解除它。首次启动及重启按失效起步，所有路径确认低于或等于恢复边界后才解除，避免重启洗掉滞回状态。缺省参数由 [`storage_health.py`](../../apps/edge-runtime/src/edge_runtime/storage_health.py) 拥有；显式值必须有限、满足 `0 ≤ recovery < high < 100` 且采样间隔为正。

中心设备页的运行镜像显示各路径最近上报的使用率、生效高水位/恢复边界和告警。事实沿既有健康队列积压及补报，不依赖中心在线；健康状态沿用旧中心已识别的 `delivering`，失效和未知状态仍按受损处理。仅首次及状态、整百分点或错误变化时入队。`edge.storage.health` 记录本机变化；SQLite 本身只读、满额或 I/O 失败时无法保证把告警持久化，运行循环记录 `edge.storage.write_failed` 并停止判定，由既有守护重启，不能继续发布通过。恢复只解除存储失效，不撤销锁存违规；探测不删除录像、在审证据或任何未确认的唯一副本。

中心主机高水位通过[媒体导出应用步骤](../../deploy/media/README.md#应用中心导出的媒体配置)进入本机 `disk_health`；改中心字段后必须重新应用并重启，本机生效值与尚未应用的中心期望值不是同一事实。该可选导出字段不改变配置束 v2、健康上报形状或 SQLite schema。

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

### 本机证据切片

可选 `evidence` 段启用本机证据切片；缺少它时运行时照常判定，证据待办保持 pending 而不声称成功。它复用 `media` 的录像目录与 ffmpeg：

| 字段 | 含义 |
|---|---|
| `evidence_directory` | 本机证据目录；每个证据一个目录，含片段/关键帧/元数据并整体原子定稿 |
| `ffprobe_binary` | 读取分段真实时长的 ffprobe |
| `slice_timeout_seconds` | 单次 ffmpeg/ffprobe 超时 |
| `recording_timezone`（可选） | 解析分段文件名的 IANA 时区；省略时按本机时区 |

分段文件名来自 MediaMTX `recordPath`，其时间戳是 MediaMTX 进程的本机时区。**部署必须保证 MediaMTX 与边缘运行时同一时区，或用 `recording_timezone` 显式声明 MediaMTX 的时区**，否则证据窗口会错位。入队时冻结录像墙钟映射与当时参与 SOP 的连续录像相机路径，重启或改绑后历史待办仍按当时来源切片。

### 本机录像压缩

可选的录像压缩归档由 `media` 的压缩字段启用；它复用 `evidence` 段已配置的 `ffprobe_binary`、`slice_timeout_seconds` 与 `recording_timezone`，因此需要同时启用证据切片，未配置证据切片的主机不执行压缩：

| 字段 | 默认 | 含义 |
|---|---|---|
| `recording_compression_enabled` | `false` | 是否为已出原始素材窗口的录像分段生成候选并在校验后原子替换 |
| `recording_compression_age_seconds` | `86400` | 原始素材窗口；整段录像时间都在窗口外的分段才可压缩 |
| `recording_compression_crf` | `28`（占位） | H.264 质量档，取值 0–51。默认档须由目标子码流实测扫档确定（§5.19 约束 5），照抄占位值不是实测结论 |

默认关闭，开启前须评估编码时原件与候选并存的空间、CRF 和资源争用。后台任务以本机 ffmpeg 生成 H.264 候选、真实校验并原子替换；提交顺序、素材占用与恢复契约见[录像压缩机制](../design/mechanisms/evidence-and-retention.md#压缩录像重编码归档证据不动记录使用-timescale-原生压缩)。通过 `edge.recording_compression.summary` 的 `created` / `replaced` / `skipped` / `failed` 观察候选生成、替换、跳过和失败；`edge.recording_compression.failed` 给出分段及失败原因。编码/校验失败移除不可用候选，替换失败保留原件与有效候选；进程退出后复用留痕和实际文件继续处理。

录像目录必须位于支持 Linux 目录 `flock`、同目录原子 rename 及文件/目录 `fsync` 的本机文件系统，不支持这些操作时告警而不绕过。压缩遇切片占用跳过本轮，锁随进程退出释放。保留 `<segment>.compressed.json` 与分段一起运维，不能单独清除；无法对应时拒绝切片/压缩。MediaMTX 的正常窗口回收与直连回放不参与该锁，窗口过期仍可能表现为素材缺失；压缩不改其回收策略，也不改证据文件和摘要。

### 本机存储分离与已上报队列裁剪

`media.recording_directory` 不得包含 `local_state_path`；启用 `evidence` 时必须与 `evidence.evidence_directory` 分离。比较使用 canonical 路径，因此 symlink 别名也会被拒绝。根配置加载只要存在 `media` 就在写入者/MediaMTX 与切片启动前拒绝这类配置；证据切片在落盘前再次执行同一 owner 校验。滚动录像的年龄回收仍由 MediaMTX 既有窗口负责，这里不新增清理守护进程，也不建立第二份文件索引。

运行时按最后确认配置的每工位 `local_reported` 有界裁剪 `local_report_queue`、`local_observation_queue`、`local_health_queue` 的逻辑行：每表每次只读取一个按主键 `queue_id` 排序的 keyset 页（最多固定行数，包含待上报行并显式记为跳过），在同一次 `BEGIN IMMEDIATE` 事务内读取最后确认配置并逐行判定。只有 `sent_at` 有权威 UTC 确认时刻（`acknowledged_at`；缺失、无时区或无法解析一律不删）、超过该工位年龄、不是该表最高 `queue_id`（避免复用已上报事件身份）、且不受未结实例/已锁存违规/待登记证据引用保护的行才被删除；待上报、未知策略/来源/确认时刻与受保护行保留并记录原因。扫描位置是本进程内存中的当前页起点，只在事务成功提交后前移；一页未满表示已到表尾并回绕到最小 `queue_id`，进程重启后从各表起点重新开始；`COMMIT` 或任何 SQLite 故障整体回滚、不采纳新位置并向上传播。该设计不依赖中心可达，也不缓存保护/资格事实。

待上报事实、待登记证据元数据、证据媒体、实例/判定/违规身份与处置去重账本一律不因裁剪删除；中心引用登记完成只结案队列项，不授权删除媒体。当前没有已定稿媒体删除路径，媒体删除保持 fail-closed。运行时日志 `edge.local_reclamation.summary` 在删除或跳过非空时记录每队列逻辑范围（`range=最小-最大`）、删除/跳过计数与原因，不代表 SQLite 物理回收量或磁盘节省。

## 当前配置变更与同步机制

`python -m edge_runtime` 仍先读取 `NVSOP_EDGE_COMMAND_CONFIG_FILE` 指向的本地 JSON。这份文件是**已实现的 bootstrap 与主机本地配置入口**：它提供中心地址、主机身份/私钥、本地推理端点与请求体、adapter profile、设备凭据、本地 SQLite 路径，以及首次无法取得中心确认配置时的自治起点；它不是中心拥有的拓扑、模板和运行参数的第二份权威。

自治运行时启动后会立即通过带主机签名的 `GET /api/v1/inference-hosts/{host_id}/configuration` 拉取当前主机的配置 bundle。中心先认证主机身份，再只组装该主机的有效后端、工位、相机、连接器/点位、模板版本和运行参数。共享配置契约只有一个当前 wire 模型：严格校验核心字段、`contract_version` 与 canonical SHA-256，不维护 v1/v2/v3 运行时分支。`config_revision` 是中心签发的单调 assignment revision；`effective_sha256` 是运行语义身份；`generated_at` 与可选 `producer` 是信封元数据，不参与运行语义身份。影响 Edge 行为的新数据必须同时声明 `required_capabilities`；当前 Edge 遇到未知 capability 会显式拒绝，不静默忽略。完整字段所有权和演进规则见[机器契约演进](../design/mechanisms/machine-contract-evolution.md)。

统一 Nginx 入口只对当前主机签名机器路径做显式白名单分流：主机配置拉取与已确认配置历史握手、monitor decision/health/observation 上报、delegated command 领取/结果回报，以及模板配置确认上报。这些路径不经过浏览器 session `auth_request`，也不使用浏览器 Cookie、Authorization 或 CSRF 身份；`X-Inference-Host-ID`、timestamp、nonce、signature 则保持原请求值并由 FastAPI 的主机签名认证最终校验。固定 `main` 开发入口的权威实现是 [`../../deploy/dev/nginx.conf`](../../deploy/dev/nginx.conf)；正式部署的随仓 Nginx 配置示例是 [`nginx-annotation.conf.example`](nginx-annotation.conf.example)，两者的机器路由白名单由部署契约测试机械保持一致。其余 `/api/v1/` 管理接口仍由现有 session + CSRF + permission 边界保护，annotation 内部授权入口和媒体网关不在该白名单内。

`LocalConfigurationStore` 只持久化**实际已经生效**的 bundle。旧循环运行时，候选只完成 host scope、revision/同 revision 冲突、能力要求，以及不读取/写入 live station state 的运行配置解析；这些步骤不会前移 durable confirmed。通过纯解析的候选才请求旧循环停机；旧工位线程全部退出后，Edge 才从最新 SQLite 状态恢复 supervisor、构造候选 composition 并切换实际 runtime，最后由 `LocalConfigurationStore` 在 SQLite 单事务确认同一候选。停机后的 composition 失败会重新从原活动配置构造 runtime、记录 `local_config_failure`，并按 revision/effective digest 在当前进程隔离该候选；中心仍返回同一失败候选时只同步诊断、不再反复停掉健康工位，候选身份变化后才重新尝试。该路径不会让候选 supervisor 与仍运行的旧 supervisor 并发写同一工位状态；本地确认失败会关闭候选工位资源，不启动新 runtime 循环，随后失败退出并从最后 durable confirmed 恢复。启动时没有旧循环并发，完整装配成功后才确认；首次无确认值且中心不可达时才使用本地 bootstrap。

maintenance loop 按 `command_poll_interval_seconds` 继续拉取。相同 revision + 相同 effective digest 的元数据刷新不重组 runtime；revision 改变时即使 effective digest 相同，也按新的 assignment 身份完成切换/确认。中心不可达不会进入实时判定进度。历史 SQLite 中已确认的配置 v1/v2 只在 `edge_runtime.local_state` 内归一化读取；共享 HTTP 契约不再接受旧 generation，下一次成功确认始终写当前格式。

因此配置变更按所有权分两条路径：中心拥有的拓扑、模板、版本、点位和运行参数通过中心数据与配置同步生效；本机推理地址、请求体、adapter profile、设备秘密等主机本地信息通过受管本地 JSON/secret 文件变更，并按部署流程重启或重新装配。不要直接修改 SQLite 来制造确认状态。

## 五个训练服务统一入口（S068）

[`deploy/dev/compose.yaml`](../../deploy/dev/compose.yaml) 是部署组合入口：原标注服务及四个原 Vendor FastAPI app 共享 `center-db/training`，以 `training_runtime` 运行，密码仅由 `training-runtime-password` secret 文件提供。四个新服务由标准 Compose `training-gpu` profile 显式启用；默认 core 构建/启动仍只运行原 CPU 服务，不运行四套 CUDA 构建或要求 NVIDIA 设备。新服务不发布宿主端口，也不是 gateway/worker 的健康前置条件。GPU 镜像保留原计算源码、CUDA 构建和 GPU 设备请求；仅部署接入不代表训练业务已开放。

业务网关只开放下列四个 **GET**，经原会话授权链要求 `dataset.dataset.view`：

- `/api/augmentation/health/ready` → `augmentation:32070`
- `/api/vlm-training/health/ready` → `vlm-training:32080`
- `/api/ddm-training/health/ready` → `ddm-training:32090`
- `/api/evaluation/health/ready` → `evaluation:32100`

每个 readiness 在原 app 进程查询原 DB engine；DB 不可达返回 503。Nginx 运行时解析固定服务名，进程/DNS 不可用返回真实网关失败，不阻塞网关启动、不回退健康响应。上述四前缀的其他路径（含原静态 health、debug、clear 和训练写操作）仍拒绝。浏览器不能指定目标 host 或上游 URL。React 标注、媒体授权、源视频上传与结果读取继续使用既有入口，没有新增通用视频代理。

四环境的冻结、构建和 CPU 部署证据命令及限制见[标注组合证据运行说明](annotation-evidence.md#训练环境冻结与统一入口验证s068)。GPU/完整生产安装兼容性、浏览器及离线制品验证不由 CPU readiness 证据替代。
