"""磁盘水位在公开采样接缝上滞回, 不删除任何安全数据。"""

import unittest
from pathlib import Path

from edge_runtime.storage_health import (
    DiskHealthConfiguration,
    DiskHealthWatch,
    DiskUsage,
    load_disk_health_configuration,
)


class DiskHealthWatchTest(unittest.TestCase):
    def test_configuration_accepts_legacy_default_and_explicit_complete_boundaries(self) -> None:
        self.assertEqual(load_disk_health_configuration(None), DiskHealthConfiguration())
        self.assertEqual(
            load_disk_health_configuration(
                {
                    "high_watermark_percent": 85,
                    "recovery_watermark_percent": 75,
                    "check_interval_seconds": 2,
                }
            ),
            DiskHealthConfiguration(85, 75, 2),
        )
        for invalid in (
            {},
            {"high_watermark_percent": 85},
            {
                "high_watermark_percent": True,
                "recovery_watermark_percent": 0,
                "check_interval_seconds": 2,
            },
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                load_disk_health_configuration(invalid)

    def test_high_holds_until_recovery_and_other_paths_cannot_clear_it(self) -> None:
        used = {"recording": 20, "evidence": 20, "local_state": 20}
        watch = DiskHealthWatch(
            paths={name: Path(name) for name in used},
            configuration=DiskHealthConfiguration(90, 80, 5),
            sample=lambda path: DiskUsage(total_bytes=100, available_bytes=100 - used[str(path)]),
        )
        initial = watch.poll(now=0)
        self.assertEqual(3, len(initial))
        self.assertFalse(any(event.impaired for event in initial))
        used["recording"] = 90
        self.assertEqual((), watch.poll(now=4))
        (high,) = watch.poll(now=5)
        self.assertEqual("recording", high.role)
        self.assertTrue(high.impaired)
        used["recording"] = 85
        used["evidence"] = 30
        self.assertEqual(
            {"recording": True, "evidence": False},
            {event.role: event.impaired for event in watch.poll(now=10)},
        )
        used["recording"] = 80
        (recovered,) = watch.poll(now=15)
        self.assertFalse(recovered.impaired)
        self.assertEqual((), watch.poll(now=20))

    def test_sampling_failure_is_impaired_and_still_requires_low_watermark(self) -> None:
        failing = True
        used = 85

        def sample(path: Path) -> DiskUsage:
            if failing:
                raise OSError("read-only filesystem")
            return DiskUsage(total_bytes=100, available_bytes=100 - used)

        watch = DiskHealthWatch(
            paths={"evidence": Path("evidence")},
            configuration=DiskHealthConfiguration(90, 80, 5),
            sample=sample,
        )
        (failed,) = watch.poll(now=0)
        self.assertTrue(failed.impaired)
        self.assertIn("read-only filesystem", failed.error or "")
        failing = False
        self.assertTrue(watch.poll(now=5)[0].impaired)
        used = 80
        self.assertFalse(watch.poll(now=10)[0].impaired)

    def test_restart_preserves_impairment_in_hysteresis_band(self) -> None:
        watch = DiskHealthWatch(
            paths={"local_state": Path("state")},
            configuration=DiskHealthConfiguration(90, 80, 5),
            sample=lambda path: DiskUsage(total_bytes=100, available_bytes=15),
            initially_impaired=True,
        )
        self.assertTrue(watch.poll(now=0)[0].impaired)

    def test_invalid_boundaries_are_rejected(self) -> None:
        for high, recovery, interval in (
            (90, 90, 5),
            (90, 95, 5),
            (100, 80, 5),
            (90, -1, 5),
            (90, 80, 0),
            (float("nan"), 80, 5),
        ):
            with (
                self.subTest(high=high, recovery=recovery, interval=interval),
                self.assertRaises(ValueError),
            ):
                DiskHealthConfiguration(high, recovery, interval)
