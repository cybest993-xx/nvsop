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
- 补丁：尚无。两处就地改造（pipeline `on_message`、处置动作）未实施。
- 备注：该提交与 §二 全部实测结论的复核基准一致（方案文档记 `6e149568` 初测、`69352021` 复核，两者 `sop-inference-bp` 零差异）。

## 已知的基座既有缺陷（不修，仅登记）

这些是 NVIDIA 交付物自带的问题。修它们需要超出 [ADR-0007](../adr/0007-base-is-the-trunk-not-a-dependency.md) 已登记范围的补丁，故原样保留并在此登记，避免被误认为我们引入。

- `agentic/vss-sop-skills/vss-sop-build/references/ds-sop-building.md` 有一个指向 `../../../vss-sop-deploy/references/build_ds_sop_image.md` 的失效相对链接。仓库策略因此不校验 `vendor/` 内的 Markdown 链接。
- `agentic/vss-sop-skills/vss-sop-build/references/deployments/{ds/ds-sop,sop}/.env` 是部署模板，秘密变量为空值或占位符（`<ngc_api_key>`、`dummy`）。仓库策略对 `vendor/` 内的 `.env` 改为按**值**校验：占位符放行，真实秘密值拦截。
- `agentic/*/references/*_reference.py` 是推理侧模块的副本，但**并非都逐字节相同**：`missing_number_detector_reference.py` 与真实模块 539 行零差异，而 `sop_step_checker_reference.py` 在 `:218` 落后一行——真实模块调用 `self.save_checker(...)`，副本换成了注释。契约测试断言真实路径，并把每个副本的漂移量记为期望值（`EXPECTED_DRIFT`），漂移变化即失败。基座更新改动真实模块时该断言预期会红，届时连同新的已验证提交一起更新期望值。
