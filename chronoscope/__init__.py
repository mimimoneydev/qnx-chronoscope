"""Chronoscope for QNX 8.0 -- prove exactly when your code runs.

A host-based determinism workbench for QNX Neutrino 8.0 systems. Simulate the
microkernel scheduler, audit deadline behaviour, auto-derive a verified fix,
and reconcile the model with real ``traceprinter`` kernel traces.

Public API::

    from chronoscope import load_model, simulate, audit, autopilot, render_html

    model = load_model("models/brake-by-wire.toml")
    trace = simulate(model)
    result = audit(trace, model)
"""

from .model import (
    DEFAULT_RR_QUANTUM_US,
    DEFAULT_TICK_US,
    DEFAULT_UNPRIVILEGED_CEILING,
    PRIO_CLOCK_IST,
    PRIO_IDLE,
    PRIO_IPI_IST,
    PRIORITY_LEVELS,
    EXAMPLE_MODEL,
    ModelError,
    SystemModel,
    dump_model,
    load_model,
    model_from_dict,
)
from .kernel import Kernel, SimulationError, simulate
from .trace import JobRecord, Segment, Trace, TraceEvent, merge_traces

__version__ = "1.0.0"
__all__ = [
    "__version__",
    "DEFAULT_RR_QUANTUM_US", "DEFAULT_TICK_US", "DEFAULT_UNPRIVILEGED_CEILING",
    "PRIO_CLOCK_IST", "PRIO_IDLE", "PRIO_IPI_IST", "PRIORITY_LEVELS",
    "EXAMPLE_MODEL", "ModelError", "SystemModel", "dump_model", "load_model",
    "model_from_dict", "Kernel", "SimulationError", "simulate",
    "JobRecord", "Segment", "Trace", "TraceEvent", "merge_traces",
    "audit", "autopilot", "render_html", "render_markdown",
]


def __getattr__(name: str):  # lazy imports keep ``import chronoscope`` cheap
    if name == "audit":
        from .audit import audit
        return audit
    if name == "autopilot":
        from .autopilot import autopilot
        return autopilot
    if name in ("render_html", "render_markdown"):
        from .report import render_html, render_markdown
        return {"render_html": render_html, "render_markdown": render_markdown}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
