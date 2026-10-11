"""导出契约必须携带由判定核心派生的原因码元数据。

OpenAPI 枚举与前端提示都源自 `ReasonCode`；本测试在导出接缝处钉住派生链的第一跳，
不连数据库、不启服务器。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from edge_runtime.judgment.reasons import ReasonCode
from scripts.export_openapi import export_openapi


class ReasonCodeMetadataExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._directory = tempfile.TemporaryDirectory()
        destination = Path(cls._directory.name) / "openapi.json"
        export_openapi(destination)
        cls.schema = json.loads(destination.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._directory.cleanup()

    def test_the_component_is_derived_from_the_core(self) -> None:
        component = self.schema["components"]["schemas"]["ReasonCode"]
        self.assertEqual("string", component["type"])
        self.assertEqual([code.value for code in ReasonCode], component["enum"])
        self.assertEqual(
            {code.value: code.hint for code in ReasonCode},
            component["x-hints"],
        )

    def test_no_operation_references_the_component(self) -> None:
        # 组件只是派生元数据，不是 wire 形状：report/SSE 的原因码保持自由 string（ADR-0003）。
        self.assertNotIn("#/components/schemas/ReasonCode", json.dumps(self.schema["paths"]))


if __name__ == "__main__":
    unittest.main()
