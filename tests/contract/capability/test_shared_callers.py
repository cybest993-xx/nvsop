"""中心与推理机在运行时引用同一个能力适配函数对象。"""

from __future__ import annotations

import unittest

from edge_runtime.connectors import writes

from factory_sop.device.usecases import points
from nvsop_contracts import unfit_for


class SharedCapabilityCallerTest(unittest.TestCase):
    def test_both_callers_use_the_package_implementation(self) -> None:
        self.assertIs(unfit_for, writes.unfit_for)
        self.assertIs(unfit_for, points.unfit_for)


if __name__ == "__main__":
    unittest.main()
