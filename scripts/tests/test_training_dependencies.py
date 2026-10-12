from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from training_dependencies import OWNER, TARGETS, VENDOR, locks, prepare

ROOT = Path(__file__).resolve().parents[2]


class TrainingLockTest(unittest.TestCase):
    def test_cr_transformer_engine_family_matches_cuda12(self) -> None:
        for target in ("cr", "cr-base"):
            packages = dict(
                line.split("==", 1)
                for line in (ROOT / OWNER / f"training-{target}.lock").read_text().splitlines()
            )
            version = packages["transformer-engine"]
            self.assertEqual(packages["transformer-engine-torch"], version)
            self.assertEqual(packages["transformer-engine-cu12"], version)
            self.assertNotIn("transformer-engine-cu13", packages)
            self.assertEqual("2.8.0+cu128", packages["torch"])

    def test_check_detects_drift_without_rewriting_any_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / OWNER).mkdir(parents=True)
            for target in TARGETS:
                (root / OWNER / f"training-{target}.lock").write_text("example==1\n")
            for content, expected in (("example==1\n", 0), ("example==2\n", 1)):

                def resolve(
                    args: list[str], *, content: str = content, **kwargs: object
                ) -> subprocess.CompletedProcess[bytes]:
                    Path(args[args.index("--output-file") + 1]).write_text(content)
                    return subprocess.CompletedProcess(args, 0)

                with patch("training_dependencies.subprocess.run", side_effect=resolve):
                    self.assertEqual(locks(root, uv="uv", write=False), expected)
                for target in TARGETS:
                    self.assertEqual(
                        (root / OWNER / f"training-{target}.lock").read_text(), "example==1\n"
                    )
            with (
                patch(
                    "training_dependencies.subprocess.run",
                    side_effect=subprocess.CalledProcessError(1, "uv"),
                ),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                locks(root, uv="uv", write=False)

    def test_generated_build_preserves_original_compute_and_constrains_every_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / OWNER).mkdir(parents=True)
            shutil.copyfile(
                ROOT / OWNER / "training-bootstrap.py", root / OWNER / "training-bootstrap.py"
            )
            for target, (service, _) in TARGETS.items():
                if service is None:
                    continue
                source = root / VENDOR / service
                source.mkdir(parents=True)
                shutil.copyfile(ROOT / VENDOR / service / "Dockerfile", source / "Dockerfile")
                shutil.copyfile(ROOT / VENDOR / service / "app.py", source / "app.py")
                (root / OWNER / f"training-{target}.lock").write_text("torchao==0.15.0\n")
            (root / OWNER / "training-cr-base.lock").write_text("torchao==0.16.0\n")
            prepare(root)
            for target, (service, _) in TARGETS.items():
                if service is None:
                    continue
                context = root / ".nvsop/artifacts/training-build" / target
                original = (ROOT / VENDOR / service / "Dockerfile").read_text()
                generated = (context / "Dockerfile").read_text()
                stages = sum(line.startswith("FROM ") for line in original.splitlines())
                self.assertEqual(generated.count("PIP_BUILD_CONSTRAINT="), stages)
                self.assertEqual(generated.count("PIP_CONSTRAINT="), stages)
                self.assertIn("MAX_JOBS=1", generated)
                self.assertNotIn("make -j$(nproc)", generated)
                self.assertEqual(
                    (context / "app.py").read_bytes(),
                    (ROOT / VENDOR / service / "app.py").read_bytes(),
                )
                for line in original.splitlines():
                    if line.startswith("COPY ") or "pip uninstall" in line:
                        self.assertIn(line, generated)
                if target == "cr":
                    self.assertEqual((context / "nvsop-base.lock").read_text(), "torchao==0.16.0\n")
                    self.assertEqual(
                        (context / "nvsop-runtime.lock").read_text(), "torchao==0.15.0\n"
                    )
