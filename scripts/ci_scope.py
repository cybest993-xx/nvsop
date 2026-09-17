#!/usr/bin/env python3
"""按两个提交之间的路径选择 CI 文档快线。"""

from __future__ import annotations

import subprocess
import sys

ROOT_DOCS = {"AGENTS.md", "CLAUDE.md", "CONTEXT.md", "README.md"}
INTEGRATION_PREFIXES = (
    "apps/control-api/",
    "apps/edge-runtime/",
    "packages/contracts/",
    "tests/system/",
    "tests/fixtures/",
    "deploy/media/",
)
INTEGRATION_FILES = {"Makefile", "uv.lock", "pyproject.toml", ".python-version"}
BROWSER_PREFIXES = ("apps/control-web/",)
BROWSER_FILES = {
    "Makefile",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    ".nvmrc",
}
MEDIA_PREFIXES = ("deploy/media/",)
MEDIA_FILES = {
    "scripts/test_media_playback.py",
    "scripts/test_whep.py",
    "tests/system/test_sys_34_media.py",
    "apps/control-web/tests/e2e/sys-34-media.spec.ts",
}


def selected(path: str, prefixes: tuple[str, ...], files: set[str]) -> bool:
    return path in files or path.startswith(prefixes)


def emit(
    *, docs_only: bool, force_all: bool, integration: bool, browser: bool, media: bool
) -> None:
    print(f"docs_only={str(docs_only).lower()}")
    print(f"force_all={str(force_all).lower()}")
    print(f"integration={str(integration).lower()}")
    print(f"browser={str(browser).lower()}")
    print(f"media={str(media).lower()}")


def resolve_commit(reference: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: ci_scope.py BASE HEAD", file=sys.stderr)
        return 2

    try:
        head = resolve_commit(argv[2])
        try:
            base = resolve_commit(argv[1])
        except subprocess.CalledProcessError:
            print("No usable base commit; running all gates.", file=sys.stderr)
            emit(docs_only=False, force_all=True, integration=True, browser=True, media=True)
            return 0
        changed = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", "-z", base, head, "--"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        ).stdout
    except (subprocess.CalledProcessError, OSError) as error:
        print(f"CI scope failed: {error}", file=sys.stderr)
        return 1

    paths = [path for path in changed.split("\0") if path]
    docs_only = bool(paths) and all(
        path in ROOT_DOCS or (path.startswith("docs/") and path.endswith(".md")) for path in paths
    )
    force_all = any(
        path.startswith(".github/")
        or path in {"Makefile", "scripts/ci_scope.py", "scripts/install_actionlint.py"}
        for path in paths
    )
    media = force_all or any(selected(path, MEDIA_PREFIXES, MEDIA_FILES) for path in paths)
    integration = (
        force_all
        or media
        or any(selected(path, INTEGRATION_PREFIXES, INTEGRATION_FILES) for path in paths)
    )
    browser = (
        force_all or media or any(selected(path, BROWSER_PREFIXES, BROWSER_FILES) for path in paths)
    )
    emit(
        docs_only=docs_only,
        force_all=force_all,
        integration=integration,
        browser=browser,
        media=media,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
