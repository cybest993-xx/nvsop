"""生成脚本必须从导出的 OpenAPI 确定性地产出前端提示常量。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts.generate_reason_codes import main


def sample_schema() -> dict[str, Any]:
    return {
        "components": {
            "schemas": {
                "ReasonCode": {
                    "type": "string",
                    "enum": ["WRONG_STEP", "STREAM_LOST"],
                    "x-hints": {
                        "WRONG_STEP": "出现了错误步骤",
                        "STREAM_LOST": "观测流中断",
                    },
                }
            }
        }
    }


EXPECTED = "\n".join(
    [
        "// 由 scripts/generate_reason_codes.py 从 packages/contracts/openapi.json 生成。",
        "// 请勿手改。原因码定义权在判定核心 edge_runtime.judgment.reasons.ReasonCode。",
        "",
        "export const REASON_CODE_HINTS: Readonly<Record<string, string>> = {",
        "  STREAM_LOST: '观测流中断',",
        "  WRONG_STEP: '出现了错误步骤',",
        "}",
        "",
    ]
)


class GenerateReasonCodesTest(unittest.TestCase):
    def test_writes_sorted_hint_constants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            openapi = Path(directory) / "openapi.json"
            destination = Path(directory) / "reasonCodes.gen.ts"
            openapi.write_text(json.dumps(sample_schema()), encoding="utf-8")

            self.assertEqual(0, main(["generate_reason_codes.py", str(openapi), str(destination)]))
            output = destination.read_text(encoding="utf-8")

        self.assertEqual(EXPECTED, output)

    def test_rejects_an_enum_value_without_a_hint(self) -> None:
        schema = sample_schema()
        del schema["components"]["schemas"]["ReasonCode"]["x-hints"]["STREAM_LOST"]
        with tempfile.TemporaryDirectory() as directory:
            openapi = Path(directory) / "openapi.json"
            destination = Path(directory) / "reasonCodes.gen.ts"
            openapi.write_text(json.dumps(schema), encoding="utf-8")
            with self.assertRaises(SystemExit):
                main(["generate_reason_codes.py", str(openapi), str(destination)])


if __name__ == "__main__":
    unittest.main()
