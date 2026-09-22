"""DDM/VLM reader adapter failure-boundary regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from factory_sop.dataset.adapters.ddm import NvidiaDdmReader
from factory_sop.dataset.adapters.vlm import NvidiaVlmReader
from factory_sop.dataset.usage import (
    DdmReaderInputError,
    DdmReaderUnavailableError,
    VlmReaderInputError,
    VlmReaderUnavailableError,
)
from factory_sop.dataset.usecases.usage import DDM_CONSUMER_PARAMETERS


@pytest.mark.parametrize(
    ("error_name", "expected_error"),
    [
        ("ValueError", DdmReaderInputError),
        ("FileNotFoundError", DdmReaderInputError),
        ("ImportError", DdmReaderUnavailableError),
        ("RuntimeError", RuntimeError),
    ],
)
def test_ddm_reader_preserves_failure_classification(
    tmp_path: Path, error_name: str, expected_error: type[BaseException]
) -> None:
    module_path = tmp_path / "ddm_reader.py"
    module_path.write_text(
        "class DDMDataset:\n"
        "    def __init__(self, **kwargs):\n"
        f"        raise {error_name}('reader failure')\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    annotation_filename = "annotation.json"
    (workspace / annotation_filename).write_text('{"video": []}', encoding="utf-8")

    with pytest.raises(expected_error):
        NvidiaDdmReader(module_path).sample_counts(
            workspace=workspace,
            annotation_filename=annotation_filename,
            parameters=dict(DDM_CONSUMER_PARAMETERS),
        )


@pytest.mark.parametrize(
    ("error_name", "expected_error"),
    [
        ("ValueError", VlmReaderInputError),
        ("ImportError", VlmReaderUnavailableError),
        ("RuntimeError", RuntimeError),
    ],
)
def test_vlm_reader_preserves_failure_classification(
    tmp_path: Path, error_name: str, expected_error: type[BaseException]
) -> None:
    module_path = tmp_path / "vlm_reader.py"
    module_path.write_text(
        "class CosmosSFTDataset:\n"
        "    def __init__(self, config, custom_config):\n"
        f"        raise {error_name}('reader failure')\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(expected_error):
        NvidiaVlmReader(module_path).validate(
            workspace=workspace,
            annotation_filename="annotation.json",
        )
