from __future__ import annotations

import io
import json
from typing import Any

from factory_sop.observability import (
    NO_CORRELATION_ID,
    configure_logging,
    correlation_scope,
    get_logger,
    new_correlation_id,
)


def emit(level: str = "info", **fields: str) -> dict[str, Any]:
    """Emit one event through a freshly configured logger and return the parsed line."""
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    logger = get_logger("template")
    getattr(logger, level)("template.version.publish.rejected", **fields)
    line: dict[str, Any] = json.loads(stream.getvalue())
    return line


def test_every_line_carries_exactly_the_five_mandatory_fields() -> None:
    # §5.15 fixes these five. Asserting on the whole object rather than field by field is
    # what makes an accidentally added or renamed field fail here instead of passing unread.
    line = emit()

    assert line == {
        "event": "template.version.publish.rejected",
        "module": "template",
        "correlation_id": NO_CORRELATION_ID,
        "level": "info",
        "ts": line["ts"],
    }


def test_the_timestamp_is_rfc3339_utc() -> None:
    # §5.15 通用约定: timestamps are UTC RFC3339 with `Z`. The Web application renders in
    # Asia/Shanghai; the log line itself never carries a local offset.
    assert emit()["ts"].endswith("Z")


def test_the_level_field_names_the_method_that_emitted_the_line() -> None:
    assert emit(level="warning")["level"] == "warning"


def test_event_specific_fields_are_kept_alongside_the_mandatory_ones() -> None:
    line = emit(template_version_id="0191...", reason="STALE_REVISION")

    assert line["template_version_id"] == "0191..."
    assert line["reason"] == "STALE_REVISION"


def test_a_level_below_the_configured_threshold_is_dropped() -> None:
    stream = io.StringIO()
    configure_logging(log_level="warning", stream=stream)

    get_logger("device").info("device.camera.health.polled")

    assert stream.getvalue() == ""


def test_a_correlation_scope_reaches_a_logger_it_never_touched() -> None:
    # The scope is what makes one request's lines findable. It must reach a logger obtained
    # elsewhere — a use case deep in a module does not receive the id as an argument.
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    logger = get_logger("auth")

    with correlation_scope("0191aaaa"):
        logger.info("auth.session.opened")

    assert json.loads(stream.getvalue())["correlation_id"] == "0191aaaa"


def test_the_scope_is_restored_when_it_ends() -> None:
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)

    with correlation_scope("0191aaaa"):
        pass
    get_logger("auth").info("auth.session.opened")

    assert json.loads(stream.getvalue())["correlation_id"] == NO_CORRELATION_ID


def test_each_generated_correlation_id_is_distinct() -> None:
    assert new_correlation_id() != new_correlation_id()
