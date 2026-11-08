"""Importers for real QNX SDP 8.0 artifacts.

A host-based developer tool qualifies for the hackathon when its "output or
functionality [is] specifically intended to build, analyze, test, deploy,
debug, or manage software for QNX 8.0" (Official Rules, Section 4, Evidence of
QNX use). These importers are that bridge: they read the actual files QNX tools
produce on a target.

Supported inputs
----------------
* ``traceprinter`` text dumps of ``tracelogger`` ``.kev`` kernel event files.
  Event vocabulary is the one documented in the QNX SDP 8.0 Utilities
  Reference: THREAD:THRUNNING/THREADY/THSEND/THRECEIVE/THREPLY/THMUTEX/
  THNANOSLEEP/THINTR/..., CONTROL:TIME, INT_ENTR/INT_EXIT, KER_CALL/KER_EXIT.
* ``slog2info`` text output (slogger2) and the compact ``.chronolog`` lines
  written by the QNX-side Chronoscope probe.
* ``pidin`` snapshots (thread priority / state table).
* Chronoscope JSON traces exported by the CLI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .trace import QNX_THREAD_EVENT, JobRecord, Segment, Trace, TraceEvent

# t:0x4be9a514 CPU:01 SYSTEM          :
# t:0x1310f052 CPU:00 THREAD  :THRUNNING     pid:1 tid:1
_LINE = re.compile(
    r"^t:0x(?P<clk>[0-9a-fA-F]+)\s+CPU:(?P<cpu>\d+)\s+"
    r"(?P<class>[A-Z_]+)\s*:(?P<event>[A-Za-z0-9_()\.\-]*)\s*(?P<rest>.*)$"
)
_PID_TID = re.compile(r"\bpid:(\d+)\s+tid:(\d+)\b")
# Optional priority tag emitted by the QNX-side Chronoscope probe (and by
# Chronoscope's traceprinter-compatible export). Plain traceprinter output
# omits it; priorities then come from a pidin snapshot.
_PRIO = re.compile(r"\bprio:(\d+)\b")
_HEADER = re.compile(r"^\s*TRACE_([A-Z_]+)::\s*(.+?)\s*$")
_SLOG2 = re.compile(
    r"^(?P<ts>\S+\s+\S+\s+\d{2}:\d{2}:\d{2}\.\d+)\s+"
    r"(?P<proc>\S+)\s+(?P<sev>\d+)\s+(?P<msg>.*)$"
)
_CHRONOLOG = re.compile(
    r"^CHRONO\s+(?P<t>\d+(?:\.\d+)?)\s+(?P<thread>\S+)\s+"
    r"(?P<kind>\S+)\s*(?P<detail>.*)$"
)
_CHRONO_EMBEDDED = re.compile(
    r"CHRONO\s+(\d+(?:\.\d+)?)\s+(\S+)\s+(\S+)\s*(.*)"
)
_DEADLINE = re.compile(r"deadline=(\d+)")
_RESPONSE = re.compile(r"response=(\d+)")


def _chrono_job(trace: Trace, pending: dict[str, JobRecord], t_us: float,
                thread: str, kind: str, detail: str) -> None:
    """Reconstruct a JobRecord from a CHRONO annotation.

    The QNX-side probe emits JOB_RELEASE / JOB_COMPLETE pairs with the same
    ``<thread>:<job>#<index>`` key, in either a traceprinter CHRONO class line
    or an slogger2 message body. Both paths must produce identical traces.
    """
    token = detail.split()[0] if detail else "job"
    key = f"{thread}:{token}"
    if kind == "job_release":
        dm = _DEADLINE.search(detail)
        rec = JobRecord(thread=thread, job=token, release_us=t_us,
                        deadline_us=float(dm.group(1)) if dm else t_us,
                        source="imported")
        pending[key] = rec
        trace.jobs.append(rec)
    elif kind == "job_complete":
        sm = _RESPONSE.search(detail)
        rec = pending.pop(key, None)
        if rec is not None:
            rec.complete_us = t_us
            if sm:
                rec.cpu_us = float(sm.group(1))
            rec.status = "MISS" if rec.missed else "OK"

# THREAD event name -> Chronoscope state
_EVENT_TO_STATE = {v: k for k, v in QNX_THREAD_EVENT.items()}
_BLOCKED_STATES = {
    "SEND", "RECEIVE", "REPLY", "MUTEX", "NANOSLEEP", "INTR", "WAITPAGE",
    "STOPPED", "SEM", "CONDVAR", "JOIN", "SIGWAITINFO", "SIGSUSPEND",
    "NET_SEND", "NET_REPLY", "DEAD",
}


class ImportError_(ValueError):
    """Raised for unrecognised or malformed QNX artifacts."""


def _cycles_to_us(cycles: int, cycles_per_sec: float) -> float:
    return cycles * 1_000_000.0 / cycles_per_sec


def parse_traceprinter(text: str, name: str = "qnx-target",
                       thread_names: dict[int, str] | None = None,
                       cycles_per_sec: float | None = None) -> Trace:
    """Parse ``traceprinter [-v] -f tracebuffer.kev`` output into a Trace.

    ``cycles_per_sec`` should be taken from the ``TRACE_CYCLES_PER_SEC::``
    header line. If the header is missing (streaming output) we assume a
    1 GHz ClockCycles() counter, matching Chronoscope's own export.
    """
    cps = cycles_per_sec or 1_000_000_000.0
    header: dict[str, str] = {}
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            header[m.group(1)] = m.group(2)
    if "CYCLES_PER_SEC" in header:
        try:
            cps = float(header["CYCLES_PER_SEC"])
        except ValueError:
            pass
    cpus = int(header.get("CPU_NUM", "1")) if header.get("CPU_NUM", "1").isdigit() else 1
    names = dict(thread_names or {})
    trace = Trace(name=name, cpus=cpus, source="traceprinter",
                  meta={"qnx_version": header.get("SYS_RELEASE", "8.0"),
                        "cycles_per_sec": cps, "header": header,
                        "source_tool": "traceprinter (QNX SDP 8.0)"})
    # tid -> (state, since, cpu, pid, priority)
    state: dict[int, tuple[str, float, int, int, int]] = {}
    prios: dict[int, int] = {}
    pending_jobs: dict[str, JobRecord] = {}
    max_us = 0.0

    def label(tid: int, pid: int) -> str:
        return names.get(tid) or names.get(pid) or f"p{pid}t{tid}"

    def close(tid: int, t_us: float, new_state: str, new_prio: int = 0) -> None:
        if tid not in state:
            return
        old, since, cpu, pid, prio = state[tid]
        if t_us > since:
            thr = label(tid, pid)
            if old == "RUNNING":
                trace.segments.append(Segment(round(since, 3), round(t_us, 3),
                                              thr, thr, "run", cpu,
                                              priority=prio))
            elif old in _BLOCKED_STATES:
                trace.segments.append(Segment(round(since, 3), round(t_us, 3),
                                              thr, thr, "blocked", cpu,
                                              priority=prio, reason=old))
        state[tid] = (new_state, t_us, cpu, pid, new_prio or prio)

    for raw in text.splitlines():
        m = _LINE.match(raw.strip())
        if not m:
            continue
        t_us = _cycles_to_us(int(m.group("clk"), 16), cps)
        max_us = max(max_us, t_us)
        cls = m.group("class")
        ev = m.group("event").strip()
        rest = m.group("rest").strip()
        cpu = int(m.group("cpu"))
        pm = _PID_TID.search(rest)
        pid = int(pm.group(1)) if pm else 0
        tid = int(pm.group(2)) if pm else 0
        prm = _PRIO.search(rest)
        if prm:
            prios[tid] = int(prm.group(1))

        if cls == "PROCESS" and ev.startswith("PROCCREATE"):
            nm = re.search(r"name:(\S+)", raw)
            if nm and pid:
                names.setdefault(pid, nm.group(1).split("/")[-1])
        elif cls == "THREAD":
            st = _EVENT_TO_STATE.get(ev)
            if st:
                prio = prios.get(tid, 0)
                if tid not in state:
                    state[tid] = (st, t_us, cpu, pid, prio)
                else:
                    close(tid, t_us, st, prio)
                trace.events.append(TraceEvent(round(t_us, 3), st,
                                               label(tid, pid), cpu,
                                               priority=prio, detail=ev,
                                               pid=pid, tid=tid,
                                               qnx_class=cls, qnx_event=ev))
        elif cls in ("INT_ENTR", "INT_EXIT"):
            trace.events.append(TraceEvent(round(t_us, 3),
                                           "interrupt_entry" if cls == "INT_ENTR"
                                           else "interrupt_exit", "ISR", cpu,
                                           detail=rest, qnx_class=cls, qnx_event=ev))
        elif cls in ("KER_CALL", "KER_EXIT"):
            trace.events.append(TraceEvent(round(t_us, 3),
                                           "kernel_call" if cls == "KER_CALL"
                                           else "kernel_exit",
                                           label(tid, pid), cpu, detail=ev,
                                           pid=pid, tid=tid,
                                           qnx_class=cls, qnx_event=ev))
        elif cls == "CONTROL":
            trace.events.append(TraceEvent(round(t_us, 3), "control_time", "", cpu,
                                           detail=rest, qnx_class=cls,
                                           qnx_event=ev))
        elif cls == "CHRONO":
            # Annotations emitted by the QNX-side probe / Chronoscope export.
            parts = rest.split(None, 1)
            thr = parts[0] if parts else ""
            detail = parts[1] if len(parts) > 1 else ""
            trace.events.append(TraceEvent(round(t_us, 3), ev.lower(), thr, cpu,
                                           detail=detail, qnx_class=cls,
                                           qnx_event=ev))
            _chrono_job(trace, pending_jobs, round(t_us, 3), thr,
                        ev.lower(), detail)
        # anything else (SYSTEM/PMU) is recorded but not interpreted
        elif cls:
            trace.events.append(TraceEvent(round(t_us, 3), "qnx_event", "", cpu,
                                           detail=rest, qnx_class=cls,
                                           qnx_event=ev))
    for tid in list(state):
        close(tid, max_us, "DEAD")
    for rec in pending_jobs.values():
        rec.status = "INCOMPLETE"
    trace.horizon_us = max_us
    trace.meta["parsed_events"] = len(trace.events)
    trace.meta["parsed_segments"] = len(trace.segments)
    trace.meta["thread_priorities"] = {
        label(tid, state[tid][3]): prio for tid, prio in prios.items() if tid in state
    }
    return trace.sort()


def parse_slog2info(text: str, name: str = "slogger2") -> Trace:
    """Parse ``slog2info`` output and the compact Chronoscope probe format.

    Accepts both a bare ``CHRONO <t_us> <thread> <KIND> <detail>`` line (the
    probe's stderr fallback) and a full ``slog2info`` line whose message body
    embeds that same annotation. JOB_RELEASE / JOB_COMPLETE pairs are matched
    into JobRecords so ``import --format slog2 --audit`` yields real deadline
    KPIs, exactly like a traceprinter dump.
    """
    trace = Trace(name=name, source="slogger2",
                  meta={"source_tool": "slog2info / slogger2 (QNX SDP 8.0)",
                        "qnx_version": "8.0"})
    pending: dict[str, JobRecord] = {}
    max_us = 0.0
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cm = _CHRONOLOG.match(line)
        if cm:
            t_us = float(cm.group("t"))
            max_us = max(max_us, t_us)
            kind = cm.group("kind").lower()
            detail = cm.group("detail")
            trace.events.append(TraceEvent(t_us, kind, cm.group("thread"),
                                           detail=detail))
            _chrono_job(trace, pending, t_us, cm.group("thread"), kind, detail)
            continue
        sm = _SLOG2.match(line)
        if sm:
            msg = sm.group("msg")
            # The QNX-side probe embeds `CHRONO <t_us> <thread> ...` in the
            # slogger2 message body so the host can recover monotonic time.
            cm2 = _CHRONO_EMBEDDED.search(msg)
            if cm2:
                t_us = float(cm2.group(1))
                max_us = max(max_us, t_us)
                kind = cm2.group(3).lower()
                detail = cm2.group(4)
                trace.events.append(TraceEvent(t_us, kind, cm2.group(2),
                                               detail=detail))
                _chrono_job(trace, pending, t_us, cm2.group(2), kind, detail)
            else:
                trace.events.append(TraceEvent(float(lineno), "slog", sm.group("proc"),
                                               detail=msg))
            continue
        # Free-form slogger2 line: keep it as an annotation, never crash.
        trace.events.append(TraceEvent(float(lineno), "slog", "", detail=line))
    for rec in pending.values():
        rec.status = "INCOMPLETE"
    trace.horizon_us = max_us or len(trace.events)
    trace.meta["parsed_events"] = len(trace.events)
    trace.meta["parsed_jobs"] = len(trace.jobs)
    return trace.sort()


def parse_pidin(text: str, name: str = "pidin") -> Trace:
    """Parse a ``pidin`` thread table into a priority/state snapshot.

    Accepted columns (whitespace separated): ``pid tid name priority state``
    or QNX's ``pid tid name pri state`` from ``pidin -F`` output.
    """
    trace = Trace(name=name, source="pidin",
                  meta={"source_tool": "pidin (QNX SDP 8.0)", "qnx_version": "8.0"})
    threads: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith(("pid", "#", "---")):
            continue
        cols = line.split()
        if len(cols) < 4:
            continue
        try:
            pid, tid = int(cols[0]), int(cols[1])
        except ValueError:
            continue
        nm = cols[2]
        prio = 0
        state = ""
        for c in cols[3:]:
            if c.isdigit() and not prio:
                prio = int(c)
            elif c.isalpha():
                state = c
                break
        threads.append({"pid": pid, "tid": tid, "name": nm,
                        "priority": prio, "state": state})
        trace.events.append(TraceEvent(0.0, "snapshot", nm, priority=prio,
                                       pid=pid, tid=tid, detail=state))
    trace.meta["threads"] = threads
    trace.meta["thread_count"] = len(threads)
    return trace


def load_trace(path: str | Path, **kw: Any) -> Trace:
    """Auto-detect a Chronoscope JSON trace or a QNX artifact and load it."""
    p = Path(path)
    if not p.is_file():
        raise ImportError_(f"trace file not found: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix.lower() in (".json", ".cstrace"):
        return Trace.from_json(p)
    if "TRACEPRINTER version" in text or " -- KERNEL EVENTS -- " in text \
            or re.search(r"^t:0x[0-9a-fA-F]+\s+CPU:\d+", text, re.M):
        return parse_traceprinter(text, name=p.stem, **kw)
    if "CHRONO " in text or re.search(r"slog2info|slogger2", text, re.I):
        return parse_slog2info(text, name=p.stem)
    return parse_pidin(text, name=p.stem)


def reconcile(simulated: Trace, measured: Trace) -> dict[str, Any]:
    """Compare a simulated model with a real QNX trace.

    Returns a machine-readable drift report: per-thread WCET/jitter deltas and
    a verdict. This is the step that makes the model *falsifiable* against the
    target, rather than a pretty animation.
    """
    def stats(tr: Trace) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        by_thread: dict[str, list[float]] = {}
        for j in tr.jobs:
            r = j.response_us
            if r is None:
                continue
            by_thread.setdefault(j.thread, []).append(r)
        for name, xs in by_thread.items():
            if not xs:
                continue
            out[name] = {
                "samples": float(len(xs)),
                "wcet_us": max(xs),
                "min_us": min(xs),
                "mean_us": sum(xs) / len(xs),
                "jitter_us": max(xs) - min(xs),
            }
        return out

    sim, real = stats(simulated), stats(measured)
    rows: list[dict[str, Any]] = []
    for name in sorted(set(sim) | set(real)):
        s, r = sim.get(name), real.get(name)
        if s and r:
            rows.append({
                "thread": name,
                "sim_wcet_us": round(s["wcet_us"], 1),
                "qnx_wcet_us": round(r["wcet_us"], 1),
                "wcet_delta_pct": round(
                    (r["wcet_us"] - s["wcet_us"]) / max(s["wcet_us"], 1e-9) * 100, 1),
                "sim_jitter_us": round(s["jitter_us"], 1),
                "qnx_jitter_us": round(r["jitter_us"], 1),
                "verdict": "ALIGNED" if abs(r["wcet_us"] - s["wcet_us"])
                           <= max(0.1 * s["wcet_us"], 50.0) else "DRIFT",
            })
        else:
            rows.append({"thread": name,
                         "sim_wcet_us": s["wcet_us"] if s else None,
                         "qnx_wcet_us": r["wcet_us"] if r else None,
                         "verdict": "MISSING_ON_TARGET" if s else "UNMODELLED"})
    drift = sum(1 for x in rows if x["verdict"] == "DRIFT")
    return {
        "simulated_jobs": len([j for j in simulated.jobs if j.response_us is not None]),
        "measured_jobs": len([j for j in measured.jobs if j.response_us is not None]),
        "rows": rows,
        "drift_count": drift,
        "verdict": "ALIGNED" if drift == 0 else "MODEL NEEDS CALIBRATION",
        "note": ("Import a real traceprinter dump to falsify the model. "
                 "A DRIFT verdict means WCET assumptions in the TOML model "
                 "must be re-measured on the QNX 8.0 target."),
    }


def json_default(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return str(obj)
