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

from check_documentation import check_documentation
from nvsop_config import load_center_modules

REQUIRED_FILES = {
    Path("AGENTS.md"),
    Path("CONTEXT.md"),
    Path("Makefile"),
    Path("docs/README.md"),
    Path("docs/engineering/workflow.md"),
    Path("docs/engineering/documentation.md"),
    Path("docs/design/solution-and-roadmap.md"),
    Path(".github/workflows/blocking-ci.yml"),
    Path("scripts/ci_scope.py"),
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
SECRET_SUFFIXES = {".key", ".pem"}
VENDOR_ROOT = Path("vendor")
NVIDIA_VENDOR_ROOT = VENDOR_ROOT / "sop-monitoring-blueprints"
LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"
SECRET_VARIABLE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*"
    r"(?:PASSWORD|SECRET|TOKEN|APIKEY|API_KEY|CREDENTIAL|PRIVATE_KEY))\s*=\s*(.*)$"
)
PLACEHOLDER_VALUE = re.compile(
    r"^(?:|dummy|none|null|todo|changeme|placeholder|<[^>]*>|\$\{[^}]*\}|your[-_a-z0-9]*)$",
    re.IGNORECASE,
)
CENTER_PYTHON_VERSION = "3.12"
# A literal `Authorization` header in a checked-in JSON file (an MCP server manifest, an
# HTTP client fixture) is a credential in Git regardless of what the file is called; only a
# `${VAR}` reference, expanded by the reader at load time, may be committed.
JSON_AUTHORIZATION_HEADER = re.compile(r'"Authorization"\s*:\s*"(?P<value>[^"]*)"')
ENVIRONMENT_REFERENCE = re.compile(r"^\s*(?:Bearer\s+)?\$\{[^}]+\}\s*$")
EDGE_APP = Path("apps/edge-runtime")
EDGE_SOURCE = EDGE_APP / "src"
EDGE_RUNTIME_PACKAGES = frozenset(
    {"connectors", "judgment", "local_state", "stream_health", "supervisor"}
)
CONTRACT_SOURCE = Path("packages/contracts/src/nvsop_contracts")
CENTER_SOURCE = Path("apps/control-api/src/factory_sop")
CENTER_COMPOSITION_ROOT = CENTER_SOURCE / "app.py"
CENTER_OWNER_SHAPE_FILES = frozenset({"api.py", "model.py", "repository.py", "usecases.py"})
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

    errors.extend(check_documentation(root, files))

    agents = root / "AGENTS.md"
    if agents.is_file() and "docs/engineering/architecture.md" not in agents.read_text():
        errors.append("AGENTS.md must point layout changes to docs/engineering/architecture.md")

    workflow = root / ".github/workflows/blocking-ci.yml"
    if workflow.is_file():
        text = workflow.read_text()
        for required_text in ("pull_request:", "make check", "CI required", "always()"):
            if required_text not in text:
                errors.append(f"blocking-ci.yml is missing required gate behavior: {required_text}")
        if "needs.scope.outputs.integration" not in text:
            errors.append("blocking-ci.yml must consume the shared ci_scope integration output")

    selector = root / "scripts/ci_scope.py"
    if selector.is_file() and any(is_under(path, Path("tests/system")) for path in files):
        selector_text = selector.read_text(encoding="utf-8")
        if '"tests/system/"' not in selector_text or '"apps/edge-runtime/"' not in selector_text:
            errors.append(
                "ci_scope.py integration selection must include tests/system/ and "
                "apps/edge-runtime/; path filtering is an optimization, not an exemption"
            )

    errors.extend(check_python_pin(root))
    errors.extend(check_web_toolchain(root, files))
    errors.extend(check_edge_runtime_isolation(root, files))
    errors.extend(check_edge_dependency_directions(root, files))
    errors.extend(check_shared_contract_isolation(root, files))
    errors.extend(check_center_modules_are_contracted(root, files))
    errors.extend(check_vendor_lfs(root, files))

    return errors


def check_vendor_lfs(root: Path, files: list[Path]) -> list[str]:
    """禁止 vendored NVIDIA 源码依赖上游 Git-LFS 对象存储。"""
    errors: list[str] = []
    for path in files:
        if not is_under(path, NVIDIA_VENDOR_ROOT):
            continue
        target = root / path
        if path.name == ".gitattributes":
            for number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
                if "filter=lfs" in line:
                    errors.append(
                        f"{path}:{number} enables Git-LFS inside the vendored NVIDIA subtree; "
                        "subtree imports do not copy upstream LFS objects"
                    )
        if target.stat().st_size <= 1024 and target.read_bytes().startswith(LFS_POINTER_PREFIX):
            errors.append(
                f"vendored NVIDIA file is a Git-LFS pointer: {path}; "
                "exclude the upstream LFS-only asset or vendor real bytes"
            )
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


def check_edge_dependency_directions(root: Path, files: list[Path]) -> list[str]:
    """补齐 import-linter 尚未覆盖的 Edge 依赖方向。"""
    edge_root = EDGE_SOURCE / "edge_runtime"
    stream_health_module = edge_root / "stream_health.py"
    stream_health_package = edge_root / "stream_health"
    composition_root = edge_root / "runtime.py"
    connector_root = edge_root / "connectors"
    write_ledger_debt = connector_root / "writes.py"
    violations: list[str] = []

    for path in sorted(files):
        if path.suffix != ".py" or not is_under(path, edge_root):
            continue
        tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=str(path))
        imports = _edge_import_targets(path, tree)
        is_stream_health = path == stream_health_module or is_under(path, stream_health_package)
        imported_packages = {
            target.split(".", 2)[1]
            for _, target in imports
            if target.startswith("edge_runtime.")
            and target.split(".", 2)[1] in EDGE_RUNTIME_PACKAGES
        }
        if path != composition_root and imported_packages >= EDGE_RUNTIME_PACKAGES:
            violations.append(
                f"{path} imports all Edge runtime packages; only edge_runtime/runtime.py may "
                "be the composition root"
            )
        for line, target in imports:
            if target == "edge_runtime" or target.startswith("edge_runtime."):
                if is_stream_health:
                    if target == "edge_runtime.stream_health" or target.startswith(
                        "edge_runtime.stream_health."
                    ):
                        continue
                    violations.append(
                        f"{path}:{line} imports {target}; stream_health is the vendor-hook "
                        "boundary and must import no edge_runtime sibling"
                    )
                    continue
                if not is_under(path, connector_root):
                    continue
                if (
                    target == "edge_runtime.connectors"
                    or target.startswith("edge_runtime.connectors.")
                    or target == "edge_runtime.judgment"
                    or target.startswith("edge_runtime.judgment.")
                ):
                    continue
                if target == "edge_runtime.supervisor.inputs" or target.startswith(
                    "edge_runtime.supervisor.inputs."
                ):
                    continue
                # #300 负责清除这条既有持久化耦合；这里只保留精确债务豁免，
                # 让 #295 阻止新增 connector ownership 泄漏而不越界实现 WriteLedger。
                if path == write_ledger_debt and target == "edge_runtime.local_state.disposal":
                    continue
                violations.append(
                    f"{path}:{line} imports {target}; connectors may depend only on judgment and "
                    "supervisor input vocabulary outside their own package"
                )
    return violations


def _edge_import_targets(path: Path, tree: ast.AST) -> list[tuple[int, str]]:
    package = ["edge_runtime", *path.relative_to(EDGE_SOURCE / "edge_runtime").parent.parts]
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            keep = len(package) - node.level + 1
            base_parts = package[: max(keep, 0)]
            if node.module:
                base_parts.extend(node.module.split("."))
            base = ".".join(base_parts)
        else:
            base = node.module or ""
        if base == "edge_runtime":
            direct_packages = [
                f"edge_runtime.{alias.name}"
                for alias in node.names
                if alias.name in EDGE_RUNTIME_PACKAGES
            ]
            if direct_packages:
                imports.extend((node.lineno, target) for target in direct_packages)
                if len(direct_packages) == len(node.names):
                    continue
        if base == "edge_runtime.supervisor" and any(
            alias.name == "inputs" for alias in node.names
        ):
            imports.extend(
                (node.lineno, "edge_runtime.supervisor.inputs")
                for alias in node.names
                if alias.name == "inputs"
            )
            if all(alias.name == "inputs" for alias in node.names):
                continue
        if base:
            imports.append((node.lineno, base))
    return sorted(imports)


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
    """要求已有生产文件的注册 Center 模块都被 import-linter 契约命名。"""
    registered = load_center_modules(root / "pyproject.toml")
    physical = {
        path.relative_to(CENTER_SOURCE).parts[0]
        for path in files
        if is_under(path, CENTER_SOURCE) and len(path.relative_to(CENTER_SOURCE).parts) > 1
    }
    modules = registered & physical
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


def center_boundary_violations(root: Path, files: list[Path]) -> list[str]:
    """评估通用 Center owner 边界，不在本任务中激活仓库阻塞门禁。"""
    registered = load_center_modules(root / "pyproject.toml")
    violations: list[tuple[str, int, str]] = []

    for path in sorted(files):
        if path.suffix != ".py" or not is_under(path, CENTER_SOURCE):
            continue
        relative = path.relative_to(CENTER_SOURCE)
        if len(relative.parts) > 1:
            namespace = relative.parts[0]
            namespace_path = Path(*relative.parts[1:])
            if namespace not in registered and _has_product_owner_shape(namespace_path):
                violations.append(
                    (
                        str(path),
                        0,
                        f"{path} gives unregistered center namespace {namespace} a "
                        "product-owner shape; ownership must be resolved explicitly rather "
                        "than inferred outside [tool.nvsop].center_modules",
                    )
                )

        if path == CENTER_COMPOSITION_ROOT or len(relative.parts) < 2:
            continue
        source_owner = relative.parts[0]
        if source_owner not in registered:
            continue

        tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=str(path))
        for line, target in _center_import_targets(path, tree):
            parts = target.split(".")
            if len(parts) < 2 or parts[0] != "factory_sop":
                continue
            target_owner = parts[1]
            if target_owner not in registered or target_owner == source_owner:
                continue
            if len(parts) >= 3 and parts[2] == "api":
                continue
            violations.append(
                (
                    str(path),
                    line,
                    f"{path}:{line} registered center module {source_owner} imports {target}; "
                    "cross-owner production imports must use "
                    f"factory_sop.{target_owner}.api",
                )
            )

    return [message for _, _, message in sorted(violations)]


def _has_product_owner_shape(path: Path) -> bool:
    return (
        path.name in CENTER_OWNER_SHAPE_FILES
        or (path.parts and path.parts[0] == "usecases")
        or path == Path("adapters/tables.py")
    )


def _center_import_targets(path: Path, tree: ast.AST) -> list[tuple[int, str]]:
    package = ["factory_sop", *path.relative_to(CENTER_SOURCE).parent.parts]
    imports: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue

        if node.level:
            keep = len(package) - node.level + 1
            base_parts = package[: max(keep, 0)]
            if node.module:
                base_parts.extend(node.module.split("."))
            base = ".".join(base_parts)
        else:
            base = node.module or ""
        if not base:
            continue

        base_parts = base.split(".")
        if base == "factory_sop" or (len(base_parts) == 2 and base_parts[0] == "factory_sop"):
            for alias in node.names:
                target = base if alias.name == "*" else f"{base}.{alias.name}"
                imports.append((node.lineno, target))
        else:
            imports.append((node.lineno, base))

    return sorted(imports)


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


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    if sys.argv[1:] not in ([], ["--docs-only"]):
        print("usage: check_repo_policy.py [--docs-only]", file=sys.stderr)
        return 2
    files = repository_files(root)
    errors = (
        check_documentation(root, files)
        if sys.argv[1:] == ["--docs-only"]
        else check_repository(root, files)
    )
    if errors:
        print("Repository policy failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Repository policy passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
