"""Shared access to the NVIDIA base code for the two contract families (§5.9).

Both families need to reach the same four standard-library-only modules under
`vendor/sop-monitoring-blueprints/`, and importing them takes care: the base is noisy on
import and most of its modules pull in pydantic, torch or DeepStream. Kept in one place so
family two does not carry a second copy of the same import dance.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import io
import logging
import os
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_ROOT = REPO_ROOT / "vendor/sop-monitoring-blueprints"
INFERENCE_ROOT = BASE_ROOT / "microservices/sop-inference-bp"
DETECTOR = INFERENCE_ROOT / "nvds_action_detector"
AGENTIC_REFERENCES = BASE_ROOT / "agentic/ds-sop-skills/deepstream-sop/references"
DDM_DATASET = (
    BASE_ROOT
    / "microservices/sop-training-bp/microservices/evaluation-ms"
    / "ddm/DDM-Net/datasets/ddm_dataset.py"
)


def read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(
            f"base file is missing: {path.relative_to(REPO_ROOT)}. "
            "Update docs/base/verified-commits.md if the base moved it."
        )
    return path.read_text(encoding="utf-8")


def import_detector(module: str) -> ModuleType:
    """Import a standard-library-only module from the base package.

    The base is noisy on import: it reads `LOG_LEVEL` for its records and prints a one-time
    banner per logger unconditionally. Both are quieted here so the gate's output stays
    readable, rather than by patching `vendor/`.
    """
    if str(INFERENCE_ROOT) not in sys.path:
        sys.path.insert(0, str(INFERENCE_ROOT))
    os.environ.setdefault("LOG_LEVEL", "ERROR")
    with contextlib.redirect_stdout(io.StringIO()):
        imported = importlib.import_module(f"nvds_action_detector.{module}")
    logging.getLogger("DS_ACTION_DETECTOR").setLevel(logging.ERROR)
    return imported


def function_source(path: Path, name: str) -> str:
    tree = ast.parse(read(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(read(path), node) or ""
    raise AssertionError(f"{path.name} no longer defines {name}()")
