"""文档链接迁移和可达性的行为回归。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_documentation import check_documentation


class DocumentationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name: str, text: str) -> Path:
        path = Path(name)
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_text(text, encoding="utf-8")
        return path

    def test_index_traverses_owners_not_unlinked_cycles_or_code_examples(self) -> None:
        files = [
            self.write("README.md", "[Docs](docs/)\n"),
            self.write("docs/README.md", "[First](first.md)\n"),
            self.write("docs/first.md", "[Second](second.md)\n"),
            self.write("docs/second.md", "[First](first.md)\n"),
            self.write("vendor/upstream/README.md", "[Broken](absent.md)\n"),
        ]
        self.assertEqual(check_documentation(self.root, files), [])
        self.write("docs/README.md", "```md\n[First](first.md)\n```\n")
        self.assertEqual(
            check_documentation(self.root, files),
            [
                "unindexed Markdown document: docs/first.md; link from a reachable owner",
                "unindexed Markdown document: docs/second.md; link from a reachable owner",
            ],
        )

    def test_fragments_follow_visible_headings_and_explicit_anchors(self) -> None:
        files = [
            self.write(
                "README.md",
                "[Guide](docs/guide.md#部署与验证)\n[Again](docs/guide.md#部署与验证-1)\n"
                "[ID](docs/guide.md#stable-id)\n[Local](#hello-world)\n# Hello, *world*!\n",
            ),
            self.write(
                "docs/guide.md",
                '# Guide\n## 部署与验证\n## 部署与验证\n<a id="stable-id"></a>\n'
                "```md\n## Not real\n```\n",
            ),
        ]
        self.assertEqual(check_documentation(self.root, files), [])
        self.write("README.md", "[Missing](docs/guide.md#not-real)\n[Local](#absent)\n")
        errors = check_documentation(self.root, files)
        self.assertTrue(any("#not-real" in error for error in errors), errors)
        self.assertTrue(any("#absent" in error for error in errors), errors)

    def test_local_ignored_material_cannot_satisfy_a_document_link(self) -> None:
        files = [self.write("README.md", "[Local file](.tmp/local.txt)\n")]
        self.write(".tmp/local.txt", "present only on this machine\n")
        errors = check_documentation(self.root, files)
        self.assertTrue(any("not in repository inputs" in error for error in errors), errors)
        files.append(Path(".tmp/local.txt"))
        self.assertEqual(check_documentation(self.root, files), [])

    def test_link_deletion_and_repository_boundary_keep_explicit_failures(self) -> None:
        files = [
            self.write(
                "README.md",
                "[Guide](docs/a%20guide.md#details)\n"
                "[Remote](https://example.invalid/not-fetched)\n"
                "[Mail](mailto:docs@example.invalid)\n"
                "```md\n[Example](not-a-real-link.md)\n```\n",
            ),
            self.write("docs/a guide.md", "# Guide\n\n## Details\n"),
        ]
        self.assertEqual(check_documentation(self.root, files), [])
        (self.root / files.pop()).unlink()
        self.write("docs/README.md", "[Outside](../../outside.md)\n")
        self.write("README.md", "[Deleted](docs/a%20guide.md)\n[Index](docs/)\n")
        files.append(Path("docs/README.md"))
        self.assertEqual(
            check_documentation(self.root, files),
            [
                "broken local Markdown link: README.md -> docs/a guide.md",
                "Markdown link escapes repository: docs/README.md -> ../../outside.md",
            ],
        )
