"""template 拥有的机器配置绑定与不可变制品投影。"""

from __future__ import annotations

from uuid import UUID

from factory_sop.template.api import (
    TemplateConfigurationError,
    TemplateConfigurationGateway,
    TemplateConfigurationProjection,
)
from factory_sop.template.repository import TemplateRepository
from nvsop_contracts import (
    ConfigurationArtifact,
    ConfigurationTemplate,
    ResolvedRuntimeParameters,
)


class RepositoryTemplateConfigurationGateway(TemplateConfigurationGateway):
    """从 template owner 的仓储构造最小机器配置投影。"""

    def __init__(self, templates: TemplateRepository) -> None:
        self._templates = templates

    def for_station(self, station_id: UUID) -> TemplateConfigurationProjection:
        binding = self._templates.binding_by_station(station_id)
        if binding is None:
            return TemplateConfigurationProjection(
                template=None,
                runtime_defaults=None,
                revision=0,
            )
        version = self._templates.version_by_id(binding.desired_version_id)
        if version is None:
            raise TemplateConfigurationError("station binding refers to a missing template version")
        template_owner = self._templates.template_by_id(version.template_id)
        if template_owner is None or template_owner.station_id != station_id:
            raise TemplateConfigurationError("station binding refers to a foreign template version")

        defaults = version.runtime_defaults
        if (
            defaults.idle_timeout_seconds is None
            or defaults.step_deadline_seconds is None
            or defaults.disposition_policy is None
        ):
            raise TemplateConfigurationError("published template has incomplete runtime defaults")
        return TemplateConfigurationProjection(
            template=ConfigurationTemplate(
                version_id=str(version.id),
                version_sha256=version.sha256,
                artifacts=tuple(
                    ConfigurationArtifact(
                        name=artifact.name.value,
                        media_type=artifact.media_type,
                        content=artifact.content,
                        sha256=artifact.sha256,
                    )
                    for artifact in version.artifacts
                ),
            ),
            runtime_defaults=ResolvedRuntimeParameters(
                idle_timeout_seconds=defaults.idle_timeout_seconds,
                step_deadline_seconds=defaults.step_deadline_seconds,
                disposition_policy=defaults.disposition_policy,
            ),
            revision=max(binding.desired_config_revision, version.source_draft_revision),
        )


__all__ = ["RepositoryTemplateConfigurationGateway"]
