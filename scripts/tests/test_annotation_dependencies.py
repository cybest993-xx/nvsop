from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from annotation_dependencies import LOCK, compile_lock


class AnnotationLockTest(unittest.TestCase):
    def test_check_detects_changed_resolution_without_rewriting_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / LOCK
            lock.parent.mkdir(parents=True)
            lock.write_text("example==1\n")
            for resolved, expected in (("example==1\n", 0), ("example==2\n", 1)):
                with self.subTest(resolved=resolved):

                    def resolve(
                        args: list[str], *, content: str = resolved, **kwargs: object
                    ) -> subprocess.CompletedProcess[bytes]:
                        Path(args[-1]).write_text(content)
                        return subprocess.CompletedProcess(args, 0)

                    with patch("annotation_dependencies.subprocess.run", side_effect=resolve):
                        self.assertEqual(expected, compile_lock(root, uv="uv", write=False))
                    self.assertEqual("example==1\n", lock.read_text())

    def test_resolution_failure_is_not_a_matching_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / LOCK
            lock.parent.mkdir(parents=True)
            lock.write_text("example==1\n")
            with (
                patch(
                    "annotation_dependencies.subprocess.run",
                    side_effect=subprocess.CalledProcessError(1, "uv"),
                ),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                compile_lock(root, uv="uv", write=False)
            self.assertEqual("example==1\n", lock.read_text())
