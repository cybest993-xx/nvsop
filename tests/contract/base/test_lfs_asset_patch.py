"""NVIDIA subtree must stay independent of upstream Git-LFS object storage."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from base_harness import BASE_ROOT, REPO_ROOT

PATCH = REPO_ROOT / "docs/base/patches/0003-drop-unavailable-lfs-assets.patch"
CLEANUP_COMMIT = "6109ef742366edb0ba4090856ca8b46cdca4993f"  # pragma: allowlist secret
PRE_CLEANUP_COMMIT = f"{CLEANUP_COMMIT}^"
REMOVED_LFS_PATHS = (
    BASE_ROOT / "agentic/ds-sop-skills/assets/DeepStream-SOP-Inference-Agentic-Workflow.png",
    BASE_ROOT
    / (
        "agentic/vss-sop-skills/vss-sop-build/references/diagrams/"
        "SOP Blueprint - VSS SOP building flow.png"
    ),
    BASE_ROOT
    / (
        "agentic/vss-sop-skills/vss-sop-build/references/diagrams/"
        "VSS SOP Blueprint Architecture.png"
    ),
    BASE_ROOT / "assets/SOP-FT-Inference-Agentic-Workflow.png",
    BASE_ROOT / "microservices/sop-inference-bp/docs/deepstream-sop-architecture.png",
    BASE_ROOT
    / (
        "microservices/sop-training-bp/microservices/ddm-training-ms/ddm/DDM-Net/config/"
        "downsample-temporal_stride.png"
    ),
    BASE_ROOT
    / (
        "microservices/sop-training-bp/microservices/evaluation-ms/ddm/DDM-Net/config/"
        "downsample-temporal_stride.png"
    ),
    BASE_ROOT / "microservices/sop-training-bp/tutorials/SOP_Training_BP_User_Guide.pdf",
)
REMOVED_ATTRIBUTE_PATHS = (
    BASE_ROOT / ".gitattributes",
    BASE_ROOT / "microservices/sop-training-bp/.gitattributes",
)


class LfsAssetPatchContractTest(unittest.TestCase):
    def test_registered_patch_replays_from_pre_cleanup_vendor_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
            subprocess.run(
                ["git", "config", "user.email", "lfs-patch@example.invalid"],
                cwd=checkout,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "LFS patch contract"],
                cwd=checkout,
                check=True,
            )

            changed = subprocess.run(
                [
                    "git",
                    "diff",
                    "--name-only",
                    "-z",
                    PRE_CLEANUP_COMMIT,
                    CLEANUP_COMMIT,
                    "--",
                    "vendor",
                ],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
            ).stdout
            for raw_path in changed.split(b"\0"):
                if not raw_path:
                    continue
                relative = Path(raw_path.decode())
                source = subprocess.run(
                    ["git", "show", f"{PRE_CLEANUP_COMMIT}:{relative}"],
                    cwd=REPO_ROOT,
                    check=True,
                    capture_output=True,
                ).stdout
                destination = checkout / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source)

            subprocess.run(["git", "add", "vendor"], cwd=checkout, check=True)
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "pre-cleanup"], cwd=checkout, check=True
            )
            applied = subprocess.run(
                ["git", "apply", "--index", str(PATCH)],
                cwd=checkout,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, applied.returncode, applied.stderr)

            for original in REMOVED_LFS_PATHS + REMOVED_ATTRIBUTE_PATHS:
                relative = original.relative_to(REPO_ROOT)
                self.assertFalse((checkout / relative).exists(), str(relative))

            for attributes in (checkout / BASE_ROOT.relative_to(REPO_ROOT)).rglob(".gitattributes"):
                self.assertNotIn("filter=lfs", attributes.read_text(encoding="utf-8"))

    def test_registered_patch_reverses_cleanly_from_current_vendor_tree(self) -> None:
        result = subprocess.run(
            ["git", "apply", "--reverse", "--check", str(PATCH)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_vendor_tree_has_no_lfs_tracking_or_removed_pointer_assets(self) -> None:
        for attributes in BASE_ROOT.rglob(".gitattributes"):
            self.assertNotIn("filter=lfs", attributes.read_text(encoding="utf-8"))
        for path in REMOVED_LFS_PATHS + REMOVED_ATTRIBUTE_PATHS:
            self.assertFalse(path.exists(), str(path.relative_to(REPO_ROOT)))


if __name__ == "__main__":
    unittest.main()
