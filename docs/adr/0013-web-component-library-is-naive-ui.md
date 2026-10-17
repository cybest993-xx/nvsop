# Web 组件库改为 Naive UI，并自建设计 token 层

## 状态

已采纳。本决定**重新打开** [`docs/design/solution-and-roadmap.md`](../design/solution-and-roadmap.md) §五的「**【已定】基础 Web 栈内选型**：Element Plus + Pinia」——重开的是其中的**组件库**一项。同条中的 **Pinia** 与「OpenAPI 生成的 TS 客户端置于 `apps/control-web/src/api/generated` 并提交，CI 校验重新生成后工作区干净」两条**不变**，不属于本决定的范围。

## 背景

`apps/control-web` 此前使用 Element Plus 的出厂主题：`src/main.ts` 只引入 `element-plus/dist/index.css`，仓库内没有全局样式表、没有设计 token、没有主题覆盖。视觉因此分散在 19 个组件各自的 2150 行 `<style scoped>` 里，直接引用 `--el-*` 变量，每个页面自己重新发明面板、卡片与表格的长相，页面之间观感不统一，无法满足产品对界面质量的要求。

问题的根因不是"选错了库"，而是**缺少一层设计地基**：没有 token、没有集中的主题覆盖点、没有跨特性共享的展示组件。因此本决定同时包含组件库更换与地基建设两件事——只换库而不建地基，同样的分散会在新库上重演。

产品对 Web 的要求（`docs/design/mechanisms/control-plane.md` §5.4）保持不变：简体中文、桌面 Chrome/Edge 当前稳定版、1366×768～1920×1080、键盘可操作、表单有标签、颜色不是唯一状态表达。

## 决策

- **组件库采用 Naive UI。** 它 Vue 3 + TypeScript 原生，主题经 `n-config-provider` 的 `GlobalThemeOverrides` 表达为纯 CSS 变量，不向全局注入样式，也不要求页面与库的内部类名对抗；这两点直接对应上面诊断的根因。组件面覆盖本产品实际用到的全部 14 类（Button/Form/Input/Select/Dialog/Tabs/Checkbox/Radio/Tag/Message 等）。
- **自建设计 token 层，作为色值与尺度的唯一来源。** token 定义在 `apps/control-web/src/styles/tokens.css`；Naive UI 的主题映射集中在 `apps/control-web/src/styles/naive-theme.ts`；全局基线（reset、字体栈、页面底色、`focus-visible`）在 `apps/control-web/src/styles/base.css`。页面不得覆盖组件库的内部类名，也不得再直接写死颜色。
- **建立跨特性共享展示组件层 `apps/control-web/src/components/`。** 页头、KPI 统计卡、面板、工具栏、数据表格、空态/加载态/错误态、状态标签由该层提供；各特性页复用而非各自实现。`docs/engineering/architecture.md` 已允许"共享身份、布局与客户端路由放在 Web 特性切片之外"，因此这不是新的模块所有权边界。
- **迁移一次性完成，不留双实现。** `AGENTS.md` 的不变量要求同一路径上不得并存两个能力实现，`docs/engineering/coding.md` 规则 5 要求重构不留下两个活跃实现。因此组件库不能按页面逐步替换：一次变更内把全部组件迁到 Naive UI 并移除 `element-plus`。
- **图标由 `lucide-vue-next` 提供。** 细线风格与目标视觉一致，按图标模块导入可 tree-shake；Naive UI 自身不提供图标集。
- **新增 `docs/engineering/frontend.md` 作为前端开发规范。** 设计语言、token 与主题层的使用规则、组件分层与目录归属、可访问性硬要求、测试与快照规则、新增/删减前端的检查清单归该文档；本 ADR 只记录决定与取舍。

## 不采用的方案

- **保留 Element Plus 做深度主题定制。** 主题系统是 SCSS 变量与 CSS 变量的混合，改造成本不低于换库，且仍需对抗其内部类名；同时并未解决"缺少共享展示组件层"这一根因。
- **Ant Design Vue。** 企业后台组件最全（Table 的固定列/展开/虚拟滚动、Upload、Tree 成熟度最高），但默认视觉离目标参考更远、需要覆盖的量更大，包体积也最大。本产品的表格与表单复杂度尚不需要它那部分能力。
- **shadcn-vue（Reka UI + Tailwind）。** 对定制外观的控制力最强，但 Table、Pagination、Upload 需自行拼装或另引 TanStack Table，工作量最大；且与"优先使用轻量维护良好的库、避免手写基础设施"的既有取向冲突（[`maintenance.md`](../engineering/maintenance.md)）。
- **在迁移期同时保留 Element Plus 与 Naive UI。** 与上述不变量冲突，且会让两个主题、两套可访问性表面和两份体积同时进入生产产物。

## 后果

- `apps/control-web/src/` 的 19 个组件文件全量迁移；`--el-*` 变量在切换后全部消失，所有 `<style scoped>` 需要重写。这是"统一视觉"工作量的主体，不是附带成本。
- `tests/integration/__snapshots__/markup.spec.ts.snap` 需要整体重定。该快照的用途是捕捉重构中丢失的 `aria-*`、标题层级与控件语义，因此必须逐条复核而非直接更新。
- 若干集成测试与 e2e 断言 Element Plus 的 CSS 类名（`.el-tabs__item`、`.el-checkbox`、`.el-tag`、`.el-select`），需改为角色/语义查询。这同时消除了对样式实现的耦合。
- 新增运行时依赖 `naive-ui` 与 `lucide-vue-next`，与锁文件同批落地。
- 组件库不再由本 ADR 之外的文档重复声明：具体版本由 `apps/control-web/package.json` 与 `pnpm-lock.yaml` 拥有。
- 本决定不改变 §5.4 的 Web 行为与可访问性基线，也不改变中心与边缘的职责划分、媒体所有权或任何后端契约。
