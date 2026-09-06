"""`device`: the topology of what is installed on the factory floor.

This slice (C4.1, issue #24) owns the physical inference host and the inference backend —
the process endpoint carrying one template configuration (edge-autonomy.md §5.10). Stations,
cameras, connectors and the delegated-command queue arrive with their own slices; the module
grows by adding what a ticket actually needs, not by scaffolding ahead of them.
"""
