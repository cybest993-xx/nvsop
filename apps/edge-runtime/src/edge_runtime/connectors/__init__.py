"""The connector runtime: where external signals become observations, and points get written.

A connector is a **configurable** integration point — a station may have none, one, or
several, and judgment must not branch on which (edge-autonomy.md §5.8). That constraint is
what shapes this package: nothing here decides anything. It produces
`edge_runtime.supervisor.inputs.ExternalSignal` values, which take the same path an action
number takes, and it executes the point writes the supervisor's disposal dispatch asks for.

Standard library only, like the rest of the inference host's autonomous unit (§5.11).
"""
