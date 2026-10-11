#!/usr/bin/env python3
"""从导出的 OpenAPI 派生前端原因码提示常量（仅标准库）。

原因码定义权在判定核心 `edge_runtime.judgment.reasons.ReasonCode`；导出脚本把它的 wire 枚举
与逐码提示注入 OpenAPI 的 `ReasonCode` 组件，本脚本据此写出 `reasonCodes.gen.ts`，
不连数据库、不引入运行时依赖。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        raise SystemExit("usage: generate_reason_codes.py OPENAPI DESTINATION")
    schema = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    component = schema["components"]["schemas"]["ReasonCode"]
    if component.get("type") != "string":
        raise SystemExit("ReasonCode 组件必须是 string 类型")
    enum = component.get("enum")
    if not isinstance(enum, list) or not enum or not all(isinstance(code, str) for code in enum):
        raise SystemExit("ReasonCode enum 必须是非空字符串列表")
    hints = component.get("x-hints")
    if not isinstance(hints, dict) or set(hints) != set(enum):
        raise SystemExit("ReasonCode x-hints 必须逐码覆盖 enum")
    for code in enum:
        hint = hints[code]
        if not isinstance(hint, str) or not hint.strip():
            raise SystemExit(f"ReasonCode {code} 缺非空提示")

    lines = [
        "// 由 scripts/generate_reason_codes.py 从 packages/contracts/openapi.json 生成。",
        "// 请勿手改。原因码定义权在判定核心 edge_runtime.judgment.reasons.ReasonCode。",
        "",
        "export const REASON_CODE_HINTS: Readonly<Record<string, string>> = {",
    ]
    for code in sorted(enum):
        literal = hints[code].replace("\\", "\\\\").replace("'", "\\'")
        lines.append(f"  {code}: '{literal}',")
    lines.append("}")
    lines.append("")
    destination = Path(argv[2])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
