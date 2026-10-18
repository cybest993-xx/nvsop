import { dateZhCN, zhCN } from 'naive-ui'

/**
 * Naive UI 的库级配置：中文 locale，去掉输入框的默认占位文本。
 *
 * `NInput` 在未设置 `placeholder` 时渲染 locale 里的「请输入」。迁移前的 Element Plus 输入框
 * 没有占位文本，保留它会让每个空字段多出一段并不表达任何约束的灰字（#518 的「不退化」）。
 * 需要占位提示的字段自己传 `placeholder`，其余保持空白。
 *
 * 与 `naive-theme.ts` 一样，这里是库级配置的唯一位置：`App.vue` 与测试装配
 * （`tests/support/naive.ts`）共用同一份，快照断言的就是生产里会出现的文本。
 */
export const appLocale = { ...zhCN, Input: { ...zhCN.Input, placeholder: '' } }
export const appDateLocale = dateZhCN
