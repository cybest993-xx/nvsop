"""NVIDIA subtree 必须保持对上游 Git-LFS 对象存储的独立。"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from base_harness import BASE_ROOT, REPO_ROOT

PATCH = REPO_ROOT / "docs/base/patches/0003-drop-unavailable-lfs-assets.patch"
CURRENT_CONTEXT_PATHS = (
    BASE_ROOT / "README.md",
    BASE_ROOT / "agentic/ds-sop-skills/README.md",
    BASE_ROOT / "microservices/sop-inference-bp/README.md",
    BASE_ROOT
    / (
        "microservices/sop-training-bp/microservices/ddm-training-ms/ddm/DDM-Net/config/"
        "config_guide.md"
    ),
    BASE_ROOT
    / (
        "microservices/sop-training-bp/microservices/evaluation-ms/ddm/DDM-Net/config/"
        "config_guide.md"
    ),
)
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
    def test_registered_patch_replays_without_repository_history(self) -> None:
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

            current_contents: dict[Path, bytes] = {}
            for source in CURRENT_CONTEXT_PATHS:
                relative = source.relative_to(REPO_ROOT)
                content = source.read_bytes()
                current_contents[relative] = content
                destination = checkout / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)

            subprocess.run(["git", "add", "vendor"], cwd=checkout, check=True)
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "current-cleanup-state"],
                cwd=checkout,
                check=True,
            )

            reversed_patch = subprocess.run(
                ["git", "apply", "--reverse", "--index", str(PATCH)],
                cwd=checkout,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, reversed_patch.returncode, reversed_patch.stderr)

            for pointer in REMOVED_LFS_PATHS:
                relative = pointer.relative_to(REPO_ROOT)
                restored = checkout / relative
                self.assertTrue(restored.is_file(), str(relative))
                self.assertTrue(
                    restored.read_text(encoding="ascii").startswith(
                        "version https://git-lfs.github.com/spec/v1\n"
                    ),
                    str(relative),
                )
            for attributes in REMOVED_ATTRIBUTE_PATHS:
                relative = attributes.relative_to(REPO_ROOT)
                restored = checkout / relative
                self.assertTrue(restored.is_file(), str(relative))
                self.assertIn("filter=lfs", restored.read_text(encoding="utf-8"))

            subprocess.run(
                ["git", "commit", "--quiet", "-m", "reconstructed-pre-cleanup-state"],
                cwd=checkout,
                check=True,
            )
            applied = subprocess.run(
                ["git", "apply", "--index", str(PATCH)],
                cwd=checkout,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, applied.returncode, applied.stderr)

            for removed in REMOVED_LFS_PATHS + REMOVED_ATTRIBUTE_PATHS:
                relative = removed.relative_to(REPO_ROOT)
                self.assertFalse((checkout / relative).exists(), str(relative))
            for relative, expected in current_contents.items():
                self.assertEqual(expected, (checkout / relative).read_bytes())

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
