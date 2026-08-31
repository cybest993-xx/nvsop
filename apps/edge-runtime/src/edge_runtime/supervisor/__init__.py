"""The supervisor: what drives the judgment core on the inference host.

The core is a pure function that declares when it needs waking (§5.18). This package is the
impure half around it: it normalizes what arrives from the inference service, the connectors
and its own timer into core events, holds the monotonic-clock timer the core asked for, and
turns what the core described into explicit commands.

It performs none of those commands. Persistence, disposal dispatch, evidence extraction and
reporting are their own tickets; this package's output is `Command` values.
"""

from __future__ import annotations

from edge_runtime.supervisor.commands import (
    ClipEvidence,
    CloseInstance,
    Command,
    EvidenceMargins,
    LatchViolation,
    RecordDecision,
    commands_for,
)
from edge_runtime.supervisor.inputs import (
    ActionRecognized,
    ExternalSignal,
    StreamHealthObserved,
    SupervisorInput,
    TimeAlignment,
    Validity,
    ValidityChanged,
)
from edge_runtime.supervisor.station import Reaction, StationSupervisor

__all__ = [
    "ActionRecognized",
    "ClipEvidence",
    "CloseInstance",
    "Command",
    "EvidenceMargins",
    "ExternalSignal",
    "LatchViolation",
    "Reaction",
    "RecordDecision",
    "StationSupervisor",
    "StreamHealthObserved",
    "SupervisorInput",
    "TimeAlignment",
    "Validity",
    "ValidityChanged",
    "commands_for",
]
