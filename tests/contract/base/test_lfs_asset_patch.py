"""NVIDIA subtree must stay independent of upstream Git-LFS object storage."""

from __future__ import annotations

import subprocess
import unittest

from base_harness import BASE_ROOT, REPO_ROOT

PATCH = REPO_ROOT / "docs/base/patches/0003-drop-unavailable-lfs-assets.patch"
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


class LfsAssetPatchContractTest(unittest.TestCase):
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
        for path in REMOVED_LFS_PATHS:
            self.assertFalse(path.exists(), str(path.relative_to(REPO_ROOT)))


if __name__ == "__main__":
    unittest.main()
