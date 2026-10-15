"""FloodPulse MVP package (milestone M0: local foundation only).

Scope at M0: payload validation, deterministic ``heuristic-v1`` risk scoring,
3 fixed zone definitions, an in-memory store and a deterministic simulator.

No AWS calls, no boto3, no network access and no frontend in this package yet.
"""

__all__ = ["config", "risk", "validation", "storage", "handler", "simulator"]
__version__ = "0.1.0"
