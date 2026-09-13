"""模板版本的确定性制品生成。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from factory_sop.template.model import (
    TEMPLATE_ARTIFACT_FORMAT_VERSION,
    OrderingMode,
    TemplateArtifactName,
    TemplateBoundaryDraft,
    TemplateRuntimeDefaults,
    TemplateStep,
    TemplateVersionArtifact,
)


@dataclass(frozen=True, slots=True)
class BuiltTemplateArtifacts:
    """一次发布要保存的制品和版本内容摘要。"""

    artifacts: tuple[TemplateVersionArtifact, ...]
    sha256: str


def build_template_artifacts(
    *,
    steps: Sequence[TemplateStep],
    ordering: OrderingMode,
    boundary: TemplateBoundaryDraft,
    runtime_defaults: TemplateRuntimeDefaults,
) -> BuiltTemplateArtifacts:
    """用固定编码从模板语义生成基座制品和完整性清单。"""
    actions = _json_bytes({"actions": [step.description for step in steps]})
    prompt_lines = [
        "There are "
        f"{len(steps)} possible steps for the SOP "
        "(Standard Operation Procedure) of the given video.",
        "What step is the operator doing?",
        *[step.description for step in steps],
    ]
    prompts = ("\n".join(prompt_lines) + "\n").encode("utf-8")
    semantic = _json_bytes(
        {
            "boundary": boundary.to_wire(),
            "format_version": TEMPLATE_ARTIFACT_FORMAT_VERSION,
            "ordering": ordering.value,
            "runtime_defaults": runtime_defaults.to_wire(),
            "steps": [step.to_wire() for step in steps],
        }
    )
    artifacts = [
        _artifact(TemplateArtifactName.ACTIONS, "application/json", actions),
        _artifact(TemplateArtifactName.VLM_PROMPTS, "text/plain", prompts),
        _artifact(TemplateArtifactName.TEMPLATE, "application/json", semantic),
    ]
    manifest = _json_bytes(
        {
            "artifacts": [
                {
                    "byte_length": artifact.byte_length,
                    "media_type": artifact.media_type,
                    "name": artifact.name.value,
                    "sha256": artifact.sha256,
                }
                for artifact in artifacts
            ],
            "format_version": TEMPLATE_ARTIFACT_FORMAT_VERSION,
        }
    )
    artifacts.append(_artifact(TemplateArtifactName.MANIFEST, "application/json", manifest))
    return BuiltTemplateArtifacts(
        artifacts=tuple(artifacts),
        sha256=_sha256(manifest),
    )


def _artifact(
    name: TemplateArtifactName, media_type: str, content: bytes
) -> TemplateVersionArtifact:
    return TemplateVersionArtifact(
        name=name,
        media_type=media_type,
        content=content,
        sha256=_sha256(content),
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
