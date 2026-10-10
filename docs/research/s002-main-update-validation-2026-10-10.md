# S002 固定 main 实例更新与恢复：本地真实验收记录

验收日期：2026-10-10。对应 [#190/S002](https://github.com/cybest993-xx/nvsop/issues/190)，覆盖该票三个 AC；不代替 #193 的跨模块浏览器验收、#194 的运行/告警/证据跨模块验收或目标硬件验收。

## 代码及环境身份

- 接受的主线：`main@0d1df412cf25b6f261da208be0abbaccd6e6fb0e`（PR [#448](https://github.com/cybest993-xx/nvsop/pull/448) 合并）。该提交的 Git Tree `1ca87f912ee9dd8b0cac3958ee235dc8f6e8fb0f` 与已通过完整 `make check`、Pi/DeepSeek 独立审查的修复候选 `09364cf007ee113205e268844e96d7a589363200` 相同。
- 验收宿主：本地 WSL/Linux、真实 Docker Compose、Tilt、PostgreSQL/TimescaleDB、Redis、Center API、Worker、Nginx、Playwright；HTTP 固定入口 `http://localhost:8443`。不是生产或目标现场环境。
- 为不影响共享开发数据，在忽略的 `.nvsop/artifacts/validation/s002-20261010/` 下从 `main@ca0781bbf402e58c418a89b79daabdf2ae114651` 建立本地测试仓库，再将经过复审的 #447 `scripts/dev.py` 实现放入 fixture。隔离改动**确实包括被测编排脚本**：将 `scripts/dev.py` 中 `PROJECT_NAME` 及 CLI 描述的 `nvsop-dev-main` 替换为 `nvsop-s002-iso`；同步修改 `Tiltfile` 的 `docker_compose(..., project_name=...)` 和 `deploy/dev/compose.yaml` 的 `name`、镜像命名及四个数据卷名。与已接受 `main@0d1df412` 对比，`scripts/dev.py` 除隔离命名和空行外不改变迁移、更新与锁逻辑；fixture 另为受控故障加入合成 Alembic 0055 建表迁移及 Dockerfile `RUN exit 42`。fixture 的提交 SHA **不是**正式 `main` SHA，不得当作生产部署或未经修改的主线构建。
- 原始机器日志、状态快照及 JSON 报告保留在上述忽略目录；可核对的交付证据见 [#190 验收评论](https://github.com/cybest993-xx/nvsop/issues/190#issuecomment-6093200891) 及 [#447 修复复验评论](https://github.com/cybest993-xx/nvsop/issues/447#issuecomment-6093701685)。原始密码、证书和测试数据卷不进入 Git。

## 验收结果

| 条件 | 真实操作、状态与证据 | 结论 |
| --- | --- | --- |
| AC1：已提交 main 普通更新 | 独立 main 从 `b51f5b34` 更新至 `f446e7b2`；运行与目标 SHA 一致、sample-data `passed`、HTTP liveness 为 200 | 通过 |
| AC1：迁移提交和源码隔离 | 已提交的合成 Alembic 0055 提交进入快照；脏 `README.md` 标记及另一个 worktree 独有文件未进入快照；后续两个连续提交也仅在提交后进入运行快照 | 通过 |
| AC2：构建失败 | 在下面的迁移失败解除、`b25373f3` 成功升级数据库到 `0055_s002` 后，另提交 Dockerfile `RUN exit 42` 的目标 `95b90957`；新版本构建失败，但**已经迁移成功**的旧运行实例 `b25373f3` 仍提供 HTTP 200，人工数据保持 | 通过 |
| AC2：迁移失败阻断服务 | 先对迁移目标 SHA `b25373f31c3196bd2b03e019a8404a2774ca45e8` 制造隔离数据库真实 `DuplicateTable` 冲突，`center-migrate` 退出 1；数据库仍 `0054`，人工保留行不变，新目标 API、Worker、网关均未启动，8443 拒绝连接 | 通过 |
| AC2：故障修复后重试 | 紧接上述迁移失败，仅删除隔离库中的人为冲突表，未执行任何数据库回滚；`dev-refresh` 对**同一目标** `b25373f3` 重试并成功迁移 `0054 → 0055_s002`，运行 `ready`，sample-data `passed`、HTTP 200、原记录未丢失。随后才执行上面的构建失败测试 | 通过 |
| AC3：提交序列与锁 | 持锁期间连续提交 `da1d1bb8`、`85692967`，运行仍保持旧 SHA；释放后只追赶最终目标。另在真实 Playwright UI 会话持有 `operation.lock` 时提交 `87341c6e`、`234cc588`，被测实例仍保持 `85692967`，退出交互式会话后更新至 `234cc588` 并恢复 `ready` | 通过 |
| AC3：测试和可查询状态 | 对 `85692967` 运行真实 `dev-smoke`，`passed`、退出码 0；交互式 Playwright 会话在锁验证后被显式 SIGINT 结束（退出码 130，**不计 UI 通过**），旧 UI 结果被标记为 `stale`；构建/迁移失败原因、目标及运行 SHA 保留在状态 JSON 和分 SHA 日志 | 通过 |

## 核验与边界

修复软件拥有者 #447 使用完整 `make check`（退出码 0）、三个新增红/绿回归用例及单独 Pi/DeepSeek `global:deepseek-v4.1-flash` high 只读 Spec + Standards 审查（PASS），随后由 Landing Queue 在 PR #448 的精确 Head 发布 `CI required=SUCCESS`、`Landing gate=SUCCESS` 并合并；本票在相同接受树上复用这些有效代码证据，并补充上表真实功能测试期间的互斥验证。

操作锁的证据包括 UI 会话期间 `test.status=running`、运行旧 SHA 与独立锁占用，退出后 `test=null`、旧结果 `stale`、运行 SHA 等于最新已提交目标。额外模拟锁测试只是辅助，不替代该真实 UI 会话。全部测试完成后，对唯一隔离项目执行 `dev.py down`，返回 `volumes_removed=false`，资源停止但数据库和人工数据卷保留；共享 `nvsop-dev-main` 的数据卷未用于故障注入。

此记录只证明 S002 所属的**本地开发固定实例**更新、互斥与故障恢复软件验收；未验证现场摄像头、生产 TLS、#193 的跨模块浏览器流程或 #194 的运行/告警/证据跨模块流程。未执行或中断的验证不能转为通过。
