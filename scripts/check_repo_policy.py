#!/usr/bin/env python3
"""Check repository layout and policy using only the Python standard library."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

REQUIRED_FILES = {
    Path("AGENTS.md"),
    Path("CONTEXT.md"),
    Path("Makefile"),
    Path("docs/design/repository-harness.md"),
    Path("docs/design/solution-and-roadmap.md"),
    Path(".github/workflows/blocking-ci.yml"),
}
FORBIDDEN_STALE_FILES = {
    Path("docs/research/control-gateway-stack-and-media-routing.md"),
    Path("docs/research/nvidia-sop-commercialization-gap-architecture-roadmap.md"),
}
ALLOWED_TOP_LEVEL_DIRS = {
    ".github",
    ".scratch",
    "apps",
    "deploy",
    "docs",
    "packages",
    "scripts",
    "tests",
    "vendor",
}
ALLOWED_APPS = {"control-api", "control-web", "edge-runtime"}
ALLOWED_ROOT_TEST_AREAS = {"contract", "fixtures", "performance", "system"}
MARKDOWN_LINK = re.compile(r"!?\[[^]]*]\(([^)]+)\)")
SECRET_SUFFIXES = {".key", ".pem"}
VENDOR_ROOT = Path("vendor")
SECRET_VARIABLE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*"
    r"(?:PASSWORD|SECRET|TOKEN|APIKEY|API_KEY|CREDENTIAL|PRIVATE_KEY))\s*=\s*(.*)$"
)
PLACEHOLDER_VALUE = re.compile(
    r"^(?:|dummy|none|null|todo|changeme|placeholder|<[^>]*>|\$\{[^}]*\}|your[-_a-z0-9]*)$",
    re.IGNORECASE,
)


def is_vendor(path: Path) -> bool:
    return path.parts[:1] == (VENDOR_ROOT.name,)


def repository_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [
        path
        for item in result.stdout.decode().split("\0")
        if item
        for path in [Path(item)]
        if (root / path).is_file()
    ]


def check_repository(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    present = set(files)

    for stale in sorted(FORBIDDEN_STALE_FILES & present):
        errors.append(
            f"superseded decision artifact must stay deleted: {stale}; "
            "merge valid facts into docs/design/solution-and-roadmap.md"
        )

    for required in sorted(REQUIRED_FILES):
        if required not in present and not (root / required).is_file():
            errors.append(f"missing required file: {required}")

    top_dirs = {path.parts[0] for path in files if len(path.parts) > 1}
    for directory in sorted(top_dirs - ALLOWED_TOP_LEVEL_DIRS):
        if not directory.startswith("."):
            errors.append(
                f"undeclared top-level directory: {directory}/ "
                "(update the harness and policy deliberately if ownership changes)"
            )

    if any(path.parts and path.parts[0] == "src" for path in files):
        errors.append("root src/ is forbidden; place production code in its owning app")

    app_names = {
        path.parts[1]
        for path in files
        if len(path.parts) > 2 and path.parts[0] == "apps"
    }
    for app in sorted(app_names - ALLOWED_APPS):
        errors.append(f"undeclared production app: apps/{app}/")

    for path in files:
        # `vendor/` is the NVIDIA base code and stays as delivered (ADR-0007), so its
        # own file names are not ours to rename. The rule that matters there is that no
        # real secret value ships, which is checked by value rather than by file name.
        if path.name == ".env" or (
            path.name.startswith(".env.") and path.name != ".env.example"
        ):
            if is_vendor(path):
                errors.extend(check_vendor_env_values(root, path))
            else:
                errors.append(
                    f"secret-like environment file must not be committed: {path}"
                )
        if path.suffix.lower() in SECRET_SUFFIXES:
            errors.append(f"private key material must not be committed: {path}")

        if path.parts and path.parts[0] == "tests" and len(path.parts) > 1:
            if path.parts[1] not in ALLOWED_ROOT_TEST_AREAS:
                errors.append(
                    f"root test has no declared cross-app owner: {path}; "
                    "use contract/, system/, performance/, or fixtures/"
                )

        is_python_test = path.suffix == ".py" and (
            path.name.startswith("test_") or path.name.endswith("_test.py")
        )
        if is_python_test and path.parts[0] in {"apps", "packages"}:
            if "tests" not in path.parts:
                errors.append(f"Python test must live in its owner's tests/ tree: {path}")

    for path in sorted(
        p for p in files if p.suffix.lower() == ".md" and not is_vendor(p)
    ):
        errors.extend(check_markdown_links(root, path))

    agents = root / "AGENTS.md"
    if agents.is_file() and "docs/design/repository-harness.md" not in agents.read_text():
        errors.append("AGENTS.md must point layout changes to the repository harness")

    workflow = root / ".github/workflows/blocking-ci.yml"
    if workflow.is_file():
        text = workflow.read_text()
        for required_text in ("pull_request:", "make check", "CI required", "always()"):
            if required_text not in text:
                errors.append(
                    f"blocking-ci.yml is missing required gate behavior: {required_text}"
                )

    return errors


def check_vendor_env_values(root: Path, path: Path) -> list[str]:
    """Fail only when a vendor environment template carries a real secret value.

    NVIDIA ships deployment templates whose secret variables are empty or hold
    placeholders. Those are safe to vendor; an assigned value would not be.
    """
    errors: list[str] = []
    text = (root / path).read_text(encoding="utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        match = SECRET_VARIABLE.match(line)
        if not match:
            continue
        name, raw_value = match.groups()
        value = raw_value.split("#", 1)[0].strip().strip("\"'")
        if not PLACEHOLDER_VALUE.match(value):
            errors.append(
                f"vendored environment file assigns a real secret value: "
                f"{path}:{number} ({name}); vendor templates must ship placeholders"
            )
    return errors


def check_markdown_links(root: Path, path: Path) -> list[str]:
    errors: list[str] = []
    text = (root / path).read_text(encoding="utf-8")
    for match in MARKDOWN_LINK.finditer(text):
        destination = match.group(1).strip().split()[0].strip("<>")
        destination = unquote(destination.split("#", 1)[0])
        if not destination or "://" in destination or destination.startswith("mailto:"):
            continue
        target = (root / path.parent / destination).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            errors.append(f"Markdown link escapes repository: {path} -> {destination}")
            continue
        if not target.exists():
            errors.append(f"broken local Markdown link: {path} -> {destination}")
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = check_repository(root, repository_files(root))
    if errors:
        print("Repository policy failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Repository policy passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
