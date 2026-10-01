# 推理机推理服务部署（Edge）

本文负责**每台推理机上推理服务容器**的 Compose 部署入口与两层边界。产品行为与安全边界在
[推理机自治](../../docs/design/mechanisms/edge-autonomy.md) 与 [ADR-0008](../../docs/adr/0008-credentials-stay-on-the-inference-host.md)；
本机运行配置字段在 [运行配置](../../docs/deployment/configuration.md)；测试选择与交付在[工程工作流](../../docs/engineering/workflow.md)。

## 两层边界

一台推理机上有两个独立生命周期，不要互相冒充：

| 层 | 入口 | 负责 |
|---|---|---|
| 本机运行时（主机进程） | `NVSOP_EDGE_COMMAND_CONFIG_FILE=<edge.json> python -m edge_runtime` | 判定核心、违规锁存、处置、证据切片、连接器、中心配置同步，以及**唯一的 MediaMTX 预览/录像** |
| 推理服务（本文件 Compose） | `docker compose -p <身份> -f deploy/edge/compose.yaml up -d` | 一份模板配置的 DeepStream + DDM + vLLM 推理端点，供本机 supervisor 通过 `API_SERVER_PORT` 消费 |

推理服务不负责判定与处置：基座 checker、声光、messaging 在此显式关闭。MediaMTX 只由主机
运行时启动；**不要**在本 Compose 里再起第二个 MediaMTX 或第二套录像，否则会出现两个录制者。

## 启动

每台主机、每个推理后端一个独立 Compose project。项目身份用 Compose 原生
`-p/--project-name`（或 `COMPOSE_PROJECT_NAME`）指定，例如 `host-01-backend-a`；
同一主机上的多个后端必须错开 `API_SERVER_PORT` 与 `NVIDIA_VISIBLE_DEVICES`：

```sh
# 主机 host-01 上的后端 A
API_SERVER_PORT=8301 NVIDIA_VISIBLE_DEVICES=0 \
MODEL_ROOT_DIR=/srv/nvsop/models \
NVSOP_EDGE_ACTION_CONFIG=/srv/nvsop/edge/actions.json \
NVSOP_EDGE_VLM_PROMPT=/srv/nvsop/edge/vlm_prompts.txt \
  docker compose -p host-01-backend-a -f deploy/edge/compose.yaml up -d
```

随后按[运行配置](../../docs/deployment/configuration.md)在本机启动 `python -m edge_runtime`。
停止只停本项目：`docker compose -p host-01-backend-a -f deploy/edge/compose.yaml down`。

## 资源与镜像

| 项 | 部署变量 | 说明 |
|---|---|---|
| 镜像 | `NV_DS_SOP_IMAGE` | 复用 vendor 基座镜像；生产由部署流程构建并固定 digest，本仓库不发布镜像标签 |
| GPU | `NVIDIA_VISIBLE_DEVICES` | 每个后端可见的 GPU；同机多后端按容量分配，不做型号分支 |
| 内网端口 | `API_SERVER_PORT` | supervisor 访问的推理端点；同机多后端唯一 |
| 模型根 | `MODEL_ROOT_DIR` | 只读挂到容器 `/models`，与基座 `DDM_MODEL_PATH`/`VLLM_MODEL_PATH` 默认前缀一致 |
| 模板/提示词 | `NVSOP_EDGE_ACTION_CONFIG`、`NVSOP_EDGE_VLM_PROMPT` | 本机只读文件，挂到基座默认 `ACTION_CONFIG_PATH`/`VLM_PROMPT_PATH` |

`DDM_MODEL_PATH` 与 `VLLM_MODEL_PATH` 由基座环境提供（`VLLM_MODEL_PATH` 无默认值，必须指向
`/models` 下的本机模型）。模型、模板与镜像都是部署期资源，容量与真实现场启动仍待
[目标环境验证矩阵](../../docs/research/target-environment-validation-matrix.md) 的现场门禁；本文件不声称现场通过。

## 安全与网络

- 主机私钥与设备凭据只由 `edge_runtime` 从本机只读 secret 文件读取，不进入本 Compose、
  Git、日志、API 响应或浏览器。
- 容器不继承部署者 shell 中的 NGC 凭据（`NGC_API_KEY` 固定为空）；模型在本机只读挂载。
- 本部署只服务本地内网：推理端点绑定在推理机自身，中心只做管理/聚合，**不需要**公网控制服务。
  预览/回放仍由推理机 MediaMTX 直连浏览器，不经中心中继。

**完成条件：** 目标主机上 `docker compose config` 解析出唯一 project 身份与预期端口/GPU，且
`python -m edge_runtime` 能连上该端点判定。无真实 GPU 时只证明配置正确，不标称现场启动通过。
