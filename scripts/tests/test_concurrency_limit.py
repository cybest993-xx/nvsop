from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from concurrency_limit import resolve


class ConcurrencyLimitTest(unittest.TestCase):
    def test_auto_scales_with_cores_and_respects_the_cap(self) -> None:
        with patch("concurrency_limit.os.cpu_count", return_value=16):
            self.assertEqual(resolve("PYTEST_WORKERS", "auto", 4), 4)
            self.assertEqual(resolve("PYTEST_WORKERS", "auto", 32), 16)

    def test_percent_request_resolves_against_cores_then_caps(self) -> None:
        with patch("concurrency_limit.os.cpu_count", return_value=8):
            self.assertEqual(resolve("PYTEST_WORKERS", "25%", 4), 2)
            self.assertEqual(resolve("PYTEST_WORKERS", "200%", 4), 4)

    def test_explicit_number_is_clamped_to_the_cap(self) -> None:
        self.assertEqual(resolve("CHECK_JOBS", "2", 4), 2)
        self.assertEqual(resolve("CHECK_JOBS", "16", 4), 4)

    def test_zero_stays_in_process_serial(self) -> None:
        self.assertEqual(resolve("PYTEST_WORKERS", "0", 4), 0)

    def test_invalid_request_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            resolve("PYTEST_WORKERS", "many", 4)


if __name__ == "__main__":
    unittest.main()
