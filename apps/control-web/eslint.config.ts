import prettier from '@vue/eslint-config-prettier'
import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import pluginVue from 'eslint-plugin-vue'
import accessibility from 'eslint-plugin-vuejs-accessibility'

// §六: 会话令牌只存在于 HttpOnly Cookie; localStorage 在任何文件都禁止读写。
const localStorageGlobals = [{ name: 'localStorage', message: '§六: 不在 localStorage 保存令牌' }]
// §六/ADR-0012: src/api 是控制面唯一入口, src/** 的其它生产代码只能经它访问控制面。
// 浏览器直连推理机 MediaMTX 的媒体流是 ADR-0012 的例外, 由 CameraMediaPanel.vue 内精确到行的
// disable 注释承担; tests/** 不在该限制内 (测试需要 stub fetch)。
const controlPlaneGlobals = [
  ...localStorageGlobals,
  { name: 'fetch', message: '§六/ADR-0012: 生产代码只经 src/api 访问控制面; 媒体直连按行豁免' },
]

export default defineConfigWithVueTs(
  { files: ['**/*.{ts,vue}'] },
  { ignores: ['dist/**', 'node_modules/**', 'src/api/generated/**'] },
  pluginVue.configs['flat/recommended'],
  vueTsConfigs.recommendedTypeChecked,
  // Q32 requires the Web to be keyboard-operable, with labelled inputs and status that is not
  // expressed by colour alone. These rules are what make that a gate rather than a review note:
  // an input without a label and a click handler without a keyboard equivalent both fail here.
  ...accessibility.configs['flat/recommended'],
  prettier,
  {
    // 未使用的 eslint-disable 视为错误: 豁免必须命中真实规则, 否则会悄悄失效。
    linterOptions: { reportUnusedDisableDirectives: 'error' },
    rules: {
      // A stub that has to accept arguments in order to type its own call record does not read
      // them. The underscore prefix is the declaration that this is deliberate.
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      // The session cookie is `HttpOnly`, so nothing in this application can read it and
      // nothing should try. `localStorage` is forbidden for the same reason §六 forbids it: a
      // token kept there survives the tab and is readable by any script on the page.
      'no-restricted-globals': ['error', ...localStorageGlobals],
      'no-restricted-properties': [
        'error',
        { object: 'window', property: 'localStorage', message: '§六: 不在 localStorage 保存令牌' },
      ],
    },
  },
  {
    // 只约束生产代码: `checkGlobalObject` 同时拒绝 `window.fetch`/`globalThis.fetch`。
    // 该块在全局块之后覆盖 `no-restricted-globals`, 所以重新声明 localStorage, 不能丢掉它。
    files: ['src/**/*.{ts,vue}'],
    ignores: ['src/api/**'],
    rules: {
      'no-restricted-globals': ['error', { globals: controlPlaneGlobals, checkGlobalObject: true }],
    },
  },
)
