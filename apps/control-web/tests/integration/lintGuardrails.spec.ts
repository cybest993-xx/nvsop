// @vitest-environment node
/**
 * Web 控制面护栏: 生产代码只能经 `src/api` 访问中心。
 *
 * 浏览器直连推理机 MediaMTX 是 ADR-0012 记录的例外, 由 `CameraMediaPanel.vue` 内精确到行的
 * disable 注释承担, 其有效性由 `make web-lint` 用真实配置验证。这里用 `calculateConfigForFile`
 * 取得某个文件生效的受限规则, 再用 ESLint 内置 `Linter` 对最小片段验证规则与全局对象, 不加载
 * TS typechecked 项目, 也不自造 AST 规则。
 */

import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { ESLint, Linter } from 'eslint'
import { describe, expect, it } from 'vitest'

const APP_ROOT = fileURLToPath(new URL('../..', import.meta.url))
const PRODUCTION_FILE = path.join(APP_ROOT, 'src/session/store.ts')
const API_FILE = path.join(APP_ROOT, 'src/api/controlPlane.ts')
const RULE = 'no-restricted-globals'

const eslint = new ESLint({
  cwd: APP_ROOT,
  overrideConfigFile: path.join(APP_ROOT, 'eslint.config.ts'),
})

const linter = new Linter()

async function effectiveConfig(filePath: string): Promise<Linter.Config> {
  const config = (await eslint.calculateConfigForFile(filePath)) as Linter.Config | undefined
  // 被忽略或未匹配的文件没有生效配置; 不能把它当成"规则通过"。
  if (!config) {
    throw new Error(`${filePath} has no effective ESLint config`)
  }
  return config
}

function restrictedGlobalsMessages(code: string, config: Linter.Config): Linter.LintMessage[] {
  const rule = config.rules?.[RULE]
  if (rule === undefined) {
    throw new Error(`${RULE} is not configured for this file`)
  }
  return linter.verify(
    code,
    [
      {
        files: ['**/*.js'],
        languageOptions: {
          ecmaVersion: 'latest',
          sourceType: 'module',
          globals: config.languageOptions?.globals ?? {},
        },
        rules: { [RULE]: rule },
      },
    ],
    { filename: 'probe.js' },
  )
}

function ruleIds(messages: Linter.LintMessage[]): string[] {
  return messages.map((message) => message.ruleId ?? '')
}

describe('control-plane fetch guardrail', () => {
  const directFetchForms = {
    'bare fetch': 'fetch("/api/v1/overview")',
    'window.fetch': 'window.fetch("/api/v1/overview")',
    'globalThis.fetch': 'globalThis.fetch("/api/v1/overview")',
  }

  it('restricts the three direct fetch forms in an ordinary production file', async () => {
    const config = await effectiveConfig(PRODUCTION_FILE)
    for (const [form, code] of Object.entries(directFetchForms)) {
      const messages = restrictedGlobalsMessages(code, config)
      expect(
        messages.some((message) => message.fatal),
        `${form} must parse`,
      ).toBe(false)
      expect(ruleIds(messages), form).toContain(RULE)
    }
  })

  it('still forbids localStorage in an ordinary production file', async () => {
    const config = await effectiveConfig(PRODUCTION_FILE)
    const messages = restrictedGlobalsMessages('localStorage.setItem("token", "x")', config)
    expect(messages.some((message) => message.fatal)).toBe(false)
    expect(ruleIds(messages)).toContain(RULE)
  })

  it('allows fetch inside the src/api entry without skipping the file', async () => {
    const config = await effectiveConfig(API_FILE)
    const fetchMessages = restrictedGlobalsMessages('fetch("/api/v1/overview")', config)
    expect(fetchMessages.some((message) => message.fatal)).toBe(false)
    expect(ruleIds(fetchMessages)).not.toContain(RULE)
    // 同一配置仍命中 localStorage, 证明片段确实被检查, fetch 放行不是解析失败或忽略文件造成的假通过。
    const localStorageMessages = restrictedGlobalsMessages('localStorage.getItem("token")', config)
    expect(ruleIds(localStorageMessages)).toContain(RULE)
  })
})
