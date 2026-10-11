import { REASON_CODE_HINTS } from './api/generated/reasonCodes.gen'

/**
 * 原因码提示的唯一前端入口：已知码取核心派生的常量，未知码回退为原始码 + 通用排障提示。
 * 未知码始终被接受，且不影响该条结论或健康事实的其余字段（ADR-0003）。
 */
export function reasonLabel(code: string): string {
  const hint = Object.hasOwn(REASON_CODE_HINTS, code) ? REASON_CODE_HINTS[code] : undefined
  return hint
    ? `${code} — ${hint}`
    : `${code} — 未知原因码：${code}；本界面版本尚无对应说明，请查阅版本说明或联系管理员。`
}
