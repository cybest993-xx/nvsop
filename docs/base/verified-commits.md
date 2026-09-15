# 基座已验证提交

基座仓库固定、版本不固定（§一、Q7）。本文件不记录"当前固定在哪个提交"，只记录**哪些提交上验证过**：每次 `git subtree add` / `git subtree pull` 的 NVIDIA 提交号、契约测试结果、补丁是否需要调整。

- 仓库：[`NVIDIA/sop-monitoring-blueprints`](https://github.com/NVIDIA/sop-monitoring-blueprints)
- 子树前缀：`vendor/sop-monitoring-blueprints/`
- 接入方式：`git subtree ... --squash`。NVIDIA 的提交历史不进入本仓库的提交图，本文件是版本台账。

更新命令：

```sh
git subtree pull --prefix=vendor/sop-monitoring-blueprints \
  https://github.com/NVIDIA/sop-monitoring-blueprints.git <ref> --squash
```

更新后必跑 `make check`（含 `tests/contract/base/`），并在本文件追加一行。

## 记录

### `69352021c2aae0ba071acd2629f5cff224d14ca6`

- 日期：2026-08-31
- 方式：`subtree add --squash`（首次接入）
- 提交主题：`[Feat][Eval] Add overlapping-window option to uniform chunking -- MR !93`
- 文件数：712
- 契约测试：第一族 7 条断言全部通过（`tests/contract/base/`）。第二族（我们自己实现的行为）随判定核心落地。
- 补丁：尚无。唯一一处就地改造（pipeline `on_message` 送出合成健康事件）未实施。
- 备注：该提交与 §二 全部实测结论的复核基准一致（方案文档记 `6e149568` 初测、`69352021` 复核，两者 `sop-inference-bp` 零差异）。

#### E4 补丁在该提交上落地（2026-08-31）

同一提交，追加记录：唯一一处就地改造已实施。

- 补丁：[`patches/0001-stream-health-events.patch`](patches/0001-stream-health-events.patch)。一个文件（`nvds_action_detector/ds_sop_process.py`）、两个追加块（一处 import、一处 `run_pipeline.on_message` 末尾的调用），零删除。
- 落点修正：[ADR-0007](../adr/0007-base-is-the-trunk-not-a-dependency.md) 早期记为"两个触点、两个文件"，实际为一处；早期指向的 `ds_3d_action_pipeline.py:779` 属命令行入口，不在服务路径上。理由与证据见该 ADR 与 [`measured-facts.md`](../design/measured-facts.md) §2.4。
- 契约测试：`tests/contract/base/` 共 35 条通过，其中 16 条为本次新增（`test_stream_health_patch.py`：补丁纯追加、补丁与工作树同步、`vendor/` 只 import 一处我们的模块、hook 调用位于回调末尾、基座消息类型与状态名未变、`DISABLE_SOP_CHECKER` 下的队列路由与消费者集合未变）。
- 补丁是否需要调整：不需要。

#### 标注接入补丁在该提交上落地（2026-09-09）

- 补丁：[`patches/0002-annotation-upload-target-and-accessibility.patch`](patches/0002-annotation-upload-target-and-accessibility.patch)。改动训练标注基座的显式目标上传、独立上下文入口、异步结果轮询、控件可访问性和标注服务端口边界；不复制时间轴或切片逻辑。
- 契约测试：`tests/contract/base/` 的 `test_annotation_patch.py` 验证补丁从当前 vendor 树可逆向干净应用、只触及登记的五个文件、上传请求在一次调用内固定目标、上下文入口存在、标注服务不发布端口，以及控件标签关联未丢失。
- 补丁是否需要调整：不需要。训练基座的产品接入仍须在上游提交变化后重新运行该族测试。

#### Git-LFS 失效资产清理（2026-09-16）

- 补丁：[`patches/0003-drop-unavailable-lfs-assets.patch`](patches/0003-drop-unavailable-lfs-assets.patch)。删除 8 个仅有 LFS pointer 的文档资产（7 个唯一 OID）以及两处 `filter=lfs` 规则，并移除对应文档嵌入。
- 原因：NVSOP 的 GitHub LFS endpoint 对 7 个 OID 全部返回 `404 Object does not exist on the server`；subtree 接入只带入 pointer Git blob，不会复制 NVIDIA 的 LFS 对象。
- 防复发：仓库策略拒绝 `vendor/sop-monitoring-blueprints/` 内的 `filter=lfs` 和 LFS pointer；`tests/contract/base/test_lfs_asset_patch.py` 验证补丁可逆向匹配当前 vendor 树及失效路径持续不存在。
- 开发快照：移除 `NVSOP_ALLOW_MISSING_OPTIONAL_LFS` 临时降级和对应白名单；任何缺失 LFS 对象都直接失败且留下审计记录，不再生成 partial pointer 快照。
- 补丁是否需要调整：后续每次 `git subtree pull` 都必须重新应用；若上游改为普通 Git bytes，可重新评估是否恢复相应文档资产。

## 已知的基座既有缺陷（不修，仅登记）

这些是 NVIDIA 交付物自带的问题。修它们需要超出 [ADR-0007](../adr/0007-base-is-the-trunk-not-a-dependency.md) 已登记范围的补丁，故原样保留并在此登记，避免被误认为我们引入。

- `agentic/vss-sop-skills/vss-sop-build/references/ds-sop-building.md` 有一个指向 `../../../vss-sop-deploy/references/build_ds_sop_image.md` 的失效相对链接。仓库策略因此不校验 `vendor/` 内的 Markdown 链接。
- `agentic/vss-sop-skills/vss-sop-build/references/deployments/{ds/ds-sop,sop}/.env` 是部署模板，秘密变量为空值或占位符（`<ngc_api_key>`、`dummy`）。仓库策略对 `vendor/` 内的 `.env` 改为按**值**校验：占位符放行，真实秘密值拦截。
- `agentic/*/references/*_reference.py` 是推理侧模块的副本，但**并非都逐字节相同**：`missing_number_detector_reference.py` 与真实模块 539 行零差异，而 `sop_step_checker_reference.py` 在 `:218` 落后一行——真实模块调用 `self.save_checker(...)`，副本换成了注释。契约测试断言真实路径，并把每个副本的漂移量记为期望值（`EXPECTED_DRIFT`），漂移变化即失败。基座更新改动真实模块时该断言预期会红，届时连同新的已验证提交一起更新期望值。
