"""第二组契约：登记的两项训练基座兼容性变更仍可重放。"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from base_harness import BASE_ROOT, REPO_ROOT, read

ANNOTATION_ROOT = BASE_ROOT / "microservices/sop-training-bp/microservices/video-annotator-ms"
TRAINING_COMPOSE = BASE_ROOT / "microservices/sop-training-bp/docker-compose.yml"
ANNOTATION_COMPOSE = ANNOTATION_ROOT / "docker-compose.yml"
INFERENCE = ANNOTATION_ROOT / "annotation_backend/inference.py"
APP = ANNOTATION_ROOT / "annotation_frontend/src/App.js"
EDITOR = ANNOTATION_ROOT / "annotation_frontend/src/components/ActionTimestampEditor.js"
PATCH = REPO_ROOT / "docs/base/patches/0002-annotation-upload-target-and-accessibility.patch"
VENDOR_FILES = (TRAINING_COMPOSE, ANNOTATION_COMPOSE, INFERENCE, APP, EDITOR)


class AnnotationPatchReplayTest(unittest.TestCase):
    """登记文件是未来基座更新时必须重新应用和核验的精确差异。"""

    def test_registered_patch_reverses_cleanly_from_the_current_vendor_tree(self) -> None:
        result = subprocess.run(
            ["git", "apply", "--reverse", "--check", str(PATCH)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            0,
            result.returncode,
            result.stderr or "登记的标注补丁已不再匹配 vendor/",
        )

    def test_registered_patch_applies_and_reverses_in_an_isolated_vendor_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            originals: dict[Path, bytes] = {}
            for source in VENDOR_FILES:
                relative = source.relative_to(REPO_ROOT)
                destination = checkout / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                originals[relative] = source.read_bytes()

            subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
            subprocess.run(
                [
                    "git",
                    "add",
                    *(str(source.relative_to(REPO_ROOT)) for source in VENDOR_FILES),
                ],
                cwd=checkout,
                check=True,
            )
            reverted = subprocess.run(
                ["git", "apply", "--reverse", str(PATCH)],
                cwd=checkout,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, reverted.returncode, reverted.stderr)
            self.assertNotIn(
                "selected_data_id = target_data_id or current_data_id",
                (checkout / INFERENCE.relative_to(REPO_ROOT)).read_text(encoding="utf-8"),
            )

            applied = subprocess.run(
                ["git", "apply", str(PATCH)],
                cwd=checkout,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, applied.returncode, applied.stderr)
            self.assertIn(
                "selected_data_id = target_data_id or current_data_id",
                (checkout / INFERENCE.relative_to(REPO_ROOT)).read_text(encoding="utf-8"),
            )
            self.assertIn(
                'htmlFor="annotation-fps"',
                (checkout / EDITOR.relative_to(REPO_ROOT)).read_text(encoding="utf-8"),
            )
            app_source = (checkout / APP.relative_to(REPO_ROOT)).read_text(encoding="utf-8")
            self.assertIn("ContextAnnotationApp", app_source)
            self.assertIn("未知标注准备状态", app_source)
            self.assertIn("cache: 'no-store'", app_source)
            for compose in (TRAINING_COMPOSE, ANNOTATION_COMPOSE):
                compose_source = (checkout / compose.relative_to(REPO_ROOT)).read_text(
                    encoding="utf-8"
                )
                services = ("annotation-backend", "annotation-frontend")
                for service in services:
                    start = compose_source.index(f"  {service}:\n")
                    next_service = re.search(r"\n  [A-Za-z][\w-]*:\n", compose_source[start + 1 :])
                    end = start + 1 + next_service.start() if next_service else len(compose_source)
                    self.assertNotIn("    ports:", compose_source[start:end])
            for relative, content in originals.items():
                self.assertEqual(content, (checkout / relative).read_bytes())

        headers = [line for line in read(PATCH).splitlines() if line.startswith("+++ ")]
        self.assertEqual(len(VENDOR_FILES), len(headers))
        self.assertEqual(
            {str(path) for path in VENDOR_FILES},
            {str(REPO_ROOT / header.removeprefix("+++ b/")) for header in headers},
        )


class ExplicitTargetUploadContractTest(unittest.TestCase):
    """产品路径不能依赖基座进程级的当前数据集。"""

    def setUp(self) -> None:
        self.source = read(INFERENCE)

    def test_upload_accepts_and_validates_one_explicit_target(self) -> None:
        for fragment in (
            "target_data_id: Optional[str] = Query(",
            "selected_data_id = target_data_id or current_data_id",
            "await postgres_db.get_data(selected_data_id, Dataset)",
            "const.VIDEO_ROOT, selected_data_id",
            'condition={"dataset_id": selected_data_id}',
            "dataset_id=selected_data_id",
        ):
            self.assertIn(fragment, self.source)

        upload_source = self.source[self.source.index("async def upload_video") :]
        upload_source = upload_source[: upload_source.index("async def", 1)]
        self.assertEqual(2, upload_source.count("current_data_id"))


class AnnotationEditorAccessibilityContractTest(unittest.TestCase):
    """复用控件的字段保持可键盘访问，且不复制其源代码。"""

    def setUp(self) -> None:
        self.source = read(EDITOR)

    def test_named_controls_have_stable_associations(self) -> None:
        for fragment in (
            '<Form.Label htmlFor="annotation-fps">',
            'id="annotation-fps"',
            "<Form.Label htmlFor={`annotation-action-${index}`}>",
            "id={`annotation-action-${index}`}",
            "aria-label={`事件 ${index + 1} 完成时间`}",
            "aria-label={`事件 ${index + 1} 完成时间（秒）`}",
            "ariaLabelForHandle={[",
            "`事件 ${index + 1} 开始时间`",
            "`事件 ${index + 1} 结束时间`",
            "htmlFor={`annotation-start-${index}`}",
            "id={`annotation-start-${index}`}",
            "htmlFor={`annotation-end-${index}`}",
            "id={`annotation-end-${index}`}",
            "cache: 'no-store'",
        ):
            self.assertIn(fragment, self.source)


if __name__ == "__main__":
    unittest.main()
