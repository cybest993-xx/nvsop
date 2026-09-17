# MediaMTX 部署与软件验证

本文负责本地媒体环境的运行步骤及原 #34 验收追溯。产品行为和安全边界在[预览与录像机制](../../docs/design/mechanisms/evidence-and-retention.md#55-预览与录像与-gpu-完全无关)；测试选择、独立审查和交付在[工程工作流](../../docs/engineering/workflow.md)。本环境使用合成 RTSP，无需 GPU、NVIDIA 推理后端或真实相机；不是生产实现替身，也不证明目标硬件容量。

## 合成媒体环境

从仓库根目录运行，需要 Docker Compose、curl；实际回放视频检查还需要 ffprobe。镜像及摘要以 [compose.yaml](compose.yaml) 为准，MediaMTX 路径和监听以 [mediamtx.yml](mediamtx.yml) 为准，CI 主机工具由 [blocking-ci.yml](../../.github/workflows/blocking-ci.yml) 配置。
该合成环境把录像分段缩短为 2 秒，只用于快速形成可回放证据；正式边缘运行时的分段时长来自本机媒体策略，不从该 smoke 配置继承。

```sh
docker compose -f deploy/media/compose.yaml up -d
```

环境提供 `synthetic` 零转码和 `cpu-transcoded` 真实 CPU 转码路径。先让环境生成完成的录像分段，再设置覆盖本次实际录制时段的 UTC 起止时间，不要照抄历史日期：

```sh
START=<recording-start-utc-rfc3339>
END=<recording-end-utc-rfc3339>
curl -fsSG http://127.0.0.1:9996/list \
  --data-urlencode path=synthetic --data-urlencode "start=$START" --data-urlencode "end=$END"
curl -fsSG http://127.0.0.1:9996/list \
  --data-urlencode path=cpu-transcoded --data-urlencode "start=$START" --data-urlencode "end=$END"
```

WebRTC 信令在 `http://127.0.0.1:8889`，ICE 为 `8189/udp`；回放监听独立使用 `http://127.0.0.1:9996`。WHEP 预览通过 `POST /<path>/whep` 交换 `application/sdp`，不是向中心 API 请求视频。该测试环境仅允许配置的 loopback origin；正式部署应配置可信 HTTPS 信令/回放与明确 Web origin，只向操作网络开放观看端口。

**完成条件：** 两路均能查询到本次真实分段，回放返回可解码视频；只有 Compose 启动或信令协商成功不算完成。检查入口是[真实 MediaMTX system 场景](../../tests/system/test_sys_34_media.py)及[浏览器媒体场景](../../apps/control-web/tests/e2e/sys-34-media.spec.ts)。测试所需的 `NVSOP_MEDIA_*` URL 必须指向本次真实环境；缺失变量导致 skip，不是通过。软件验收完整性还须按下表核对，不能从这两个文件的存在推断所有场景已完成。
仓库自动入口 `make media-system` 会为宿主 RTSP/WebRTC/ICE/playback 选择临时空闲端口，启动独立 Compose project、等待两路真实分段、把本次 `/list`/`/get` URL 注入上述 system 场景并在结束后清理自己的测试卷；CI 只在媒体相关输入变化时运行它。WHEP 对应使用 `make web-e2e-whep` 的独立真实协议夹具。直接手工执行 `docker compose` 时仍使用上文的默认端口。

测试结束仅停止本环境：

```sh
docker compose -f deploy/media/compose.yaml down
```

一次性测试录像确认不再需要时才在该命令加 `-v` 删除此 Compose 的 named volumes；不要对生产或固定开发实例套用清理命令。

## 应用中心导出的媒体配置

先按[本机运行配置](../../docs/deployment/configuration.md)准备完整 `edge.json`，在其 `media` 中填写[本机策略示例](edge-media-policy.example.json)要求的二进制、监听、录像目录、时长、转码线程和 origin。示例只提供媒体策略，不是完整可启动配置。设备凭据留在推理机，以只读 secret 文件挂载导出的 `credential_files` 路径。

中心导出提供本机拓扑，不提供秘密。用[配置应用脚本](../../scripts/apply_media_export.py)合并到本机设置：

```sh
python3 scripts/apply_media_export.py center-export.json edge.json edge.next.json
```

缩短已应用录像窗口时，改为带确认参数的调用；脚本会依据实际分段估算影响并记录操作员确认：

```sh
python3 scripts/apply_media_export.py --confirm-retention --operator "$USER" \
  center-export.json edge.json edge.next.json
```

只在命令成功、输出已核对后替换配置，并在安装了本机运行时的环境按部署流程启动：

```sh
mv edge.next.json edge.json
NVSOP_EDGE_COMMAND_CONFIG_FILE=edge.json python3 -m edge_runtime
```

脚本保留本机媒体策略，仅更新导出的主机/相机拓扑。新相机按参与 SOP 执行处理，既有明确的调试相机 `sop_execution: false` 保留；输出原子写入并使用 `0600`。相机 path 稳定取 `camera-<uuid hex>`，不从显示名或任意文件路径生成。

**完成条件：** 已配置媒体在本机实际运行，重复应用不产生重复 path/进程；失败和窗口确认取消不冒充生效，密码不进入中心、浏览器或日志。本机媒体导出应用不另造自动拉取循环；中心拥有的完整运行配置同步由现有配置束协议负责。

## 软件验收场景

以下保留原 #34 的全部软件场景 ID 与可观察结果，供回归和验收映射，不是测试通过记录。规则权威仍是机制文档；执行时从现有 device 用例、边缘媒体、脚本与浏览器测试复用证据，真实协议风险必须连接 MediaMTX。按工作流选择最小必要验证，不再要求所有新功能先补一轮普遍 TDD。

| ID | 前提与动作 | 必须证明的结果 |
|---|---|---|
| SYS-34-01 | 相机/推理机离线；授权用户保存配置，再提交非法地址、模式、窗口或过期 revision | 合法配置可保存且不伪造在线；非法输入有字段错误，并发编辑不丢数据 |
| SYS-34-02 | 两台主机各有相机，查询媒体描述并按主机导出 | 只含本机拓扑、稳定 path、直接目的地址；无秘密、任意文件路径或推测端口 |
| SYS-34-03 | 合成 RTSP 分别使用零转码与 CPU 转码；预览并回看 | 两路真实出帧和回放，视频直连推理机，判定主码流不变 |
| SYS-34-04 | 仅预览模式从无人观看到播放，再关闭全部播放器 | 按需拉源，到等待期后释放，无遗留转码进程或重复会话 |
| SYS-34-05 | 连续录像模式启动后不打开页面，再播放并退出 | 首次观看之前和全部退出后仍产生可回看分段 |
| SYS-34-06 | 应用不同合法窗口；缩短时取消、确认、估算失败或变更基准配置 | 先展示真实影响；取消/失败/过期确认保留旧值；有效确认只影响后续清理，无固定 7 天或录像目录外删除 |
| SYS-34-07 | 查询并播放多个实际区间、缺口和过期时段 | UTC 查询与本地展示正确，缺口、无录像、过期不伪装成连续画面 |
| SYS-34-08 | 信令成功但无解码帧，随后恢复输入或手动重试 | 首帧失败有终点，真实出帧才显示播放中，无无限等待/重复会话 |
| SYS-34-09 | 多路中断一路并恢复，另一路持续输入 | 故障只影响本路，不产生 SOP 不通过或伪造流健康事件 |
| SYS-34-10 | 未完成请求时切换、停止或离开页面，旧响应迟到 | 旧画面不能复活，连接、请求和重试全部释放 |
| SYS-34-11 | 已有配置与录像，切断中心并重启媒体入口 | 本地继续运行且历史可回放；不承诺未加载的中心 Web 离线启动 |
| SYS-34-12 | 匿名、只查看、只编辑用户，以及已知会话失效/权限撤销 | 用例与 Web 权限一致，只读不能编辑；拒绝后停止相关操作，不向媒体传中心凭据 |
| SYS-34-13 | HTTPS 页面直连，另用错误证书、错误 origin 或外部跳转 | 正常路径出帧回看；不安全或不兼容条件明确失败，不中继绕过或降低校验 |
| SYS-34-14 | 错主机、缺 secret、非法路径、转码启动失败；再应用原配置 | 旧配置保持或恢复，失败不伪装生效，无重复 path、密码泄漏或命令注入 |
| SYS-34-15 | 键盘操作多路/详看/停止/重试/回看，两个基准视口 | 焦点和标签可用，状态不只靠颜色；保留已有快照/浏览器证据 |
| SYS-34-16 | 媒体慢读、断开或转码失败，同时驱动判定 | 判定不等待播放器或媒体故障处理，硬件延迟与容量仍待实测 |
| SYS-34-17 | 子对象仍 active 时停用主机，另停用工位/相机；应用并重启 | 对应取流与新录制停止且不自动恢复；历史回放保留，恢复需显式有效配置 |
| SYS-34-18 | SOP 相机切为仅预览，或为仅预览相机启动 SOP；另用非 SOP 调试相机 | 前两者拒绝，不削弱连续素材；已确认非 SOP 相机可按需预览并明确不录像 |

Chrome/Edge、1366×768 与 1920×1080、键盘、焦点、文本错误与状态是原软件验收的一部分。合成输入或记录过的浏览器 adapter seam 可以验证故障逻辑，但不能声称真相机、现场网络、目标 GPU 或容量已通过。

## 原始验收与后置验证

原始 AC 原文保留，软件场景留在本功能验收，关联票只承载被允许后置的完整跨模块/现场证据。任何关单都须核对实际实现、适用测试、独立审查与合并；存在后置链接本身不构成完成。

| AC | 原文 | 软件场景 | 后置验证票 |
|---|---|---|---|
| AC1 | 每路相机可配置零转码或转码路径，浏览器使用推理机地址获取视频而非经中心中继。 | 01、02、03、12、13、14 | #78、#82 |
| AC2 | mediamtx 按需拉流并生成可配置滚动窗口的分段录像；保留时长来自配置而非代码常量。 | 04、05、06、07、11、14、17、18；按需/持续的分别适用条件见机制文档 | #78、#80 |
| AC3 | Web 支持多路状态、首帧失败和断流恢复提示，并满足键盘操作和非纯颜色表达。 | 08、09、10、12、15 | #78、#82 |
| AC4 | 软件与自动测试不写死海康型号、B 帧结论或并发容量；这些结果仅进入发布验证证据。 | 03、16，以及配置/测试不写硬件结论 | #77、#78 |

源记录是 [Issue #34](https://github.com/cybest993-xx/nvsop/issues/34) 及 Git 历史中的 `issue-34-spec.md`。该旧 spec 在基线 `5325952` 曾记录三个追溯待办：#77 缺少到 #34 的原生依赖、#78 缺 AC4 正文、#82 缺 AC1/AC3 正文。它们的当前处置须查实时 Issue，不因本次文档迁移视为已修复。验证依赖软件，不反向阻塞可独立实施的软件。

本功能不接管 #52/#53 的证据切片和复核、#55–#57 的分类保留/跨模块引用保护/磁盘水位/压缩，也不裁决 Q25/Q36。滚动录像不是证据制品。生产不引入逐观看者 JWT、公网分发、原生移动端、音频对讲或第二套录像索引/录制实现；中心不转发运行态视频。现场实测要求见[目标环境矩阵](../../docs/research/target-environment-validation-matrix.md)。
