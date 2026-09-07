#!/usr/bin/env python3
"""Check repository layout and policy using only the Python standard library."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote

REQUIRED_FILES = {
    Path("AGENTS.md"),
    Path("CONTEXT.md"),
    Path("Makefile"),
    Path("docs/design/repository-harness.md"),
    Path("docs/design/solution-and-roadmap.md"),
    Path(".github/workflows/blocking-ci.yml"),
    # The center backend's frozen toolchain: the pin, the workspace root, and the lockfile
    # CI installs from with `uv sync --frozen` (solution-and-roadmap.md §六).
    Path(".python-version"),
    Path("pyproject.toml"),
    Path("uv.lock"),
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
CENTER_PYTHON_VERSION = "3.12"
# Harness §5: 500 lines is the review target; 800 is the hard threshold at which an authored
# production file must extract a cohesive private module or carry a documented exception. This
# is deliberately a file budget, not a total-size cap on the product module that owns the file.
# The threshold is language-neutral, so `.vue` and `.ts` are counted beside `.py`.
MODULE_FILE_REVIEW_TARGET = 500
MODULE_FILE_LINE_LIMIT = 800
# A rare cohesive file may exceed the hard threshold only through a bounded, reviewed exception.
# Every entry added here carries a nearby comment pointing to the ADR or harness decision that
# explains why extraction would damage ownership or invariants. The ceiling prevents an exception
# from becoming permission for unbounded growth.
MODULE_FILE_LINE_EXCEPTIONS: dict[Path, int] = {}
PRODUCTION_SOURCE_ROOTS = (Path("apps"), Path("packages"))
# Generated output has one documented source command (harness §8) and is not authored, so its
# file sizes are the generator's business, not the budget's.
GENERATED_SOURCE = Path("apps/control-web/src/api/generated")
# The authored production languages this repository ships. `.vue` single-file components are
# modules exactly as much as `.py` files; `.d.ts` declarations under `src/` are hand-written
# here (the generated ones sit under `GENERATED_SOURCE`).
MODULE_FILE_SUFFIXES = frozenset({".py", ".ts", ".vue"})
# A literal `Authorization` header in a checked-in JSON file (an MCP server manifest, an
# HTTP client fixture) is a credential in Git regardless of what the file is called; only a
# `${VAR}` reference, expanded by the reader at load time, may be committed.
JSON_AUTHORIZATION_HEADER = re.compile(r'"Authorization"\s*:\s*"(?P<value>[^"]*)"')
ENVIRONMENT_REFERENCE = re.compile(r"^\s*(?:Bearer\s+)?\$\{[^}]+\}\s*$")
EDGE_APP = Path("apps/edge-runtime")
EDGE_SOURCE = EDGE_APP / "src"
CONTRACT_SOURCE = Path("packages/contracts/src/nvsop_contracts")
CENTER_SOURCE = Path("apps/control-api/src/factory_sop")
WEB_APP = Path("apps/control-web")
# What the web workspace's frozen toolchain is made of (harness §2). Each is required only
# once `apps/control-web/` exists, because §2 equally forbids adding them before it does.
WEB_TOOLCHAIN_FILES = {
    Path(".nvmrc"): (
        "harness §2 requires the Node runtime pinned in one place, as .python-version pins "
        "the interpreter"
    ),
    Path("pnpm-lock.yaml"): (
        "harness §2 requires a committed lockfile that CI installs from without updating"
    ),
    Path("pnpm-workspace.yaml"): (
        "harness §2 requires one root workspace file; a member with its own lockfile resolves "
        "separately from the rest"
    ),
    Path("package.json"): "harness §2 requires a root manifest that pins the package manager",
}
# `pnpm@11.22.0` pins; `pnpm@^11.22.0` and `pnpm@11` do not. Corepack accepts a range, and
# with one CI resolves a different package manager than a developer runs.
PACKAGE_MANAGER_PIN = re.compile(r"^[a-z]+@\d+\.\d+\.\d+$")
# `sys.stdlib_module_names` is the interpreter's own answer, so this set needs no
# maintenance as the standard library grows.
STANDARD_LIBRARY = frozenset(sys.stdlib_module_names)


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

    app_names = {path.parts[1] for path in files if len(path.parts) > 2 and path.parts[0] == "apps"}
    for app in sorted(app_names - ALLOWED_APPS):
        errors.append(f"undeclared production app: apps/{app}/")

    for path in files:
        # `vendor/` is the NVIDIA base code and stays as delivered (ADR-0007), so its
        # own file names are not ours to rename. The rule that matters there is that no
        # real secret value ships, which is checked by value rather than by file name.
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            if is_vendor(path):
                errors.extend(check_vendor_env_values(root, path))
            else:
                errors.append(f"secret-like environment file must not be committed: {path}")
        if path.suffix.lower() in SECRET_SUFFIXES:
            errors.append(f"private key material must not be committed: {path}")
        if path.suffix == ".json" and not is_vendor(path):
            errors.extend(check_json_authorization_headers(root, path))
        if (
            path.suffix in MODULE_FILE_SUFFIXES
            and is_production_source(path)
            and not is_under(path, GENERATED_SOURCE)
        ):
            errors.extend(check_module_file_size(root, path))

        if (
            path.parts
            and path.parts[0] == "tests"
            and len(path.parts) > 1
            and path.parts[1] not in ALLOWED_ROOT_TEST_AREAS
        ):
            errors.append(
                f"root test has no declared cross-app owner: {path}; "
                "use contract/, system/, performance/, or fixtures/"
            )

        is_python_test = path.suffix == ".py" and (
            path.name.startswith("test_") or path.name.endswith("_test.py")
        )
        if is_python_test and path.parts[0] in {"apps", "packages"} and "tests" not in path.parts:
            errors.append(f"Python test must live in its owner's tests/ tree: {path}")

    for path in sorted(p for p in files if p.suffix.lower() == ".md" and not is_vendor(p)):
        errors.extend(check_markdown_links(root, path))

    agents = root / "AGENTS.md"
    if agents.is_file() and "docs/design/repository-harness.md" not in agents.read_text():
        errors.append("AGENTS.md must point layout changes to the repository harness")

    workflow = root / ".github/workflows/blocking-ci.yml"
    if workflow.is_file():
        text = workflow.read_text()
        for required_text in ("pull_request:", "make check", "CI required", "always()"):
            if required_text not in text:
                errors.append(f"blocking-ci.yml is missing required gate behavior: {required_text}")
        if any(is_under(path, Path("tests/system")) for path in files):
            integration_filter = re.search(
                r"integration-gate:.*?grep -E '([^']+)'",
                text,
                flags=re.DOTALL,
            )
            if integration_filter is None or "tests/system/" not in integration_filter.group(1):
                errors.append(
                    "blocking-ci.yml integration path filter must include tests/system/; "
                    "path filtering is not an exemption (harness §7)"
                )

    errors.extend(check_python_pin(root))
    errors.extend(check_web_toolchain(root, files))
    errors.extend(check_edge_runtime_isolation(root, files))
    errors.extend(check_shared_contract_isolation(root, files))
    errors.extend(check_center_modules_are_contracted(root, files))

    return errors


def check_python_pin(root: Path) -> list[str]:
    """The center backend is anchored to one interpreter version, not to a range."""
    pin = root / ".python-version"
    if not pin.is_file():
        return []
    pinned = pin.read_text(encoding="utf-8").strip()
    if pinned != CENTER_PYTHON_VERSION:
        return [
            f".python-version must pin the center backend to {CENTER_PYTHON_VERSION}, not {pinned}"
        ]
    return []


def check_web_toolchain(root: Path, files: list[Path]) -> list[str]:
    """The web workspace's runtime and package manager are pinned, and its lockfile committed.

    Harness §2 fixes both halves of the rule: pin the runtime and the package-manager version
    with a committed lockfile CI installs from without updating, and do not add these manifests
    before a real workspace exists. So every check here is conditional on the workspace being
    there — which is also what lets the same function express the second half by staying silent.
    """
    if not any(is_under(path, WEB_APP) for path in files):
        return []

    errors: list[str] = []
    for required, reason in WEB_TOOLCHAIN_FILES.items():
        if not (root / required).is_file():
            errors.append(f"{WEB_APP}/ exists but {required} does not; {reason}")

    manifest = root / "package.json"
    if manifest.is_file():
        try:
            declared = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            return [*errors, f"package.json is not valid JSON: {error}"]
        pinned = declared.get("packageManager")
        if pinned is None:
            errors.append(
                "package.json must declare packageManager so corepack installs the pinned pnpm"
            )
        elif not PACKAGE_MANAGER_PIN.fullmatch(str(pinned)):
            errors.append(
                f'package.json must pin packageManager to one exact version, not "{pinned}"'
            )
    return errors


def check_edge_runtime_isolation(root: Path, files: list[Path]) -> list[str]:
    """Keep the inference host's package standard-library-only, mechanically.

    `edge-autonomy.md` §5.11 makes this a hard rule, and the harness keeps it for
    testability: the judgment core has to stay runnable on a bare CPU inside the NVIDIA
    base container, whose interpreter we do not choose. Two things could erode it
    silently — the package joining the center's uv workspace, which would put every center
    dependency on its import path, and a third-party import added while a developer's
    environment happens to have that package. Both are checked here rather than left to
    review.
    """
    errors: list[str] = []

    workspace_members = (
        read_toml(root / "pyproject.toml")
        .get("tool", {})
        .get("uv", {})
        .get("workspace", {})
        .get("members", [])
    )
    if any(str(EDGE_APP) == str(member).rstrip("/") for member in workspace_members):
        errors.append(
            f"{EDGE_APP} must stay out of the uv workspace; its judgment core is "
            "standard-library-only (edge-autonomy.md §5.11)"
        )

    edge_manifest = EDGE_APP / "pyproject.toml"
    declared = read_toml(root / edge_manifest).get("project", {}).get("dependencies", [])
    if declared:
        errors.append(
            f"{edge_manifest} declares dependencies; the inference host's package is "
            "standard-library-only (edge-autonomy.md §5.11)"
        )

    for path in sorted(files):
        if path.suffix != ".py" or not is_under(path, EDGE_SOURCE):
            continue
        for name in sorted(top_level_imports(root / path)):
            if name in STANDARD_LIBRARY or name in {"edge_runtime", "nvsop_contracts"}:
                continue
            errors.append(
                f"{path} imports {name}, which is not in the standard library; the "
                "inference host's package is standard-library-only "
                "(edge-autonomy.md §5.11)"
            )
    return errors


def check_shared_contract_isolation(root: Path, files: list[Path]) -> list[str]:
    """共享契约必须继续可由推理机的裸标准库进程导入。"""
    errors: list[str] = []
    for path in sorted(files):
        if path.suffix != ".py" or not is_under(path, CONTRACT_SOURCE):
            continue
        for name in sorted(top_level_imports(root / path)):
            if name in STANDARD_LIBRARY or name == "nvsop_contracts":
                continue
            errors.append(
                f"{path} imports {name}, which is not in the standard library; shared "
                "contracts imported by the inference host must stay standard-library-only"
            )
    return errors


def check_center_modules_are_contracted(root: Path, files: list[Path]) -> list[str]:
    """Every center module must be named by an `import-linter` contract.

    Module boundaries are enforced mechanically, not by review (harness §3). A module that
    no contract names has no enforced boundary, and the omission is invisible: the gate
    still passes, because `lint-imports` only checks the contracts it was given. This makes
    the missing contract itself the failure, so it lands in the same change as the module.
    """
    modules = {
        path.relative_to(CENTER_SOURCE).parts[0]
        for path in files
        if is_under(path, CENTER_SOURCE) and len(path.relative_to(CENTER_SOURCE).parts) > 1
    }
    if not modules:
        return []

    contracts = (
        read_toml(root / "pyproject.toml")
        .get("tool", {})
        .get("importlinter", {})
        .get("contracts", [])
    )
    contracted = {
        module
        for module in modules
        for contract in contracts
        if f"factory_sop.{module}" in repr(contract)
    }
    return [
        f"center module {module} has no import-linter contract in pyproject.toml; "
        "a module whose boundary is not named by a contract is unenforced"
        for module in sorted(modules - contracted)
    ]


def is_under(path: Path, directory: Path) -> bool:
    return path.parts[: len(directory.parts)] == directory.parts


def read_toml(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def top_level_imports(path: Path) -> set[str]:
    """Return the top-level package name of every absolute import in `path`.

    Relative imports resolve inside the package itself and are not reported.
    """
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".", 1)[0])
    return names


def is_production_source(path: Path) -> bool:
    """A file under an app's or package's `src/` tree that is not itself a test."""
    return (
        any(is_under(path, source_root) for source_root in PRODUCTION_SOURCE_ROOTS)
        and "src" in path.parts
        and "tests" not in path.parts
    )


def check_module_file_size(root: Path, path: Path) -> list[str]:
    """Fail at the hard production-file threshold from harness §5.

    The lower review target is guidance, not a reason to split a cohesive file mechanically.
    At the hard threshold the owner either extracts a focused private module or records why the
    file must remain whole; neither choice changes the product module's behavior or table owner.
    """
    lines = (root / path).read_text(encoding="utf-8").count("\n")
    ceiling = MODULE_FILE_LINE_EXCEPTIONS.get(path, MODULE_FILE_LINE_LIMIT)
    if lines < ceiling:
        return []
    if path in MODULE_FILE_LINE_EXCEPTIONS:
        return [
            f"{path} is {lines} lines and reaches its documented hard ceiling of {ceiling} "
            "lines; extract a cohesive private module or approve a new bounded exception "
            "(harness §5)"
        ]
    return [
        f"{path} is {lines} lines; production files at {MODULE_FILE_LINE_LIMIT} lines require "
        "extraction of a cohesive private module or a documented exception (harness §5)"
    ]


def check_json_authorization_headers(root: Path, path: Path) -> list[str]:
    """Fail when a JSON file carries a literal Authorization header value."""
    errors: list[str] = []
    text = (root / path).read_text(encoding="utf-8", errors="replace")
    for match in JSON_AUTHORIZATION_HEADER.finditer(text):
        if ENVIRONMENT_REFERENCE.match(match.group("value")):
            continue
        line = text.count("\n", 0, match.start()) + 1
        errors.append(
            f"{path}:{line} commits a literal Authorization header; reference the "
            'credential as "${VAR}" and supply it from the environment'
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
