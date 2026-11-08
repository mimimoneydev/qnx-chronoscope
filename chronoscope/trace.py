"""Execution traces produced by the Chronoscope simulator or imported from QNX.

A :class:`Trace` is the single currency of the whole tool: the simulator writes
one, the QNX importers produce one from a real ``traceprinter`` dump, the audit
consumes one, the autopilot compares them, and the report renders them.

The text export deliberately mirrors the output of the QNX SDP 8.0
``traceprinter`` utility (Utilities Reference, "Display the contents of the
instrumented kernel trace file"), including the exact event names:

    THREAD :THRUNNING / THREADY / THSEND / THRECEIVE / THREPLY / THMUTEX /
             THNANOSLEEP / THINTR / THSTOPPED / THDEAD
    CONTROL :TIME
    INT_ENTR / INT_EXIT
    KER_CALL / KER_EXIT

so a simulated trace and a real trace can be diffed with the same tooling.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

PRIO_IPI_IST = 255  # QNX 8.0 reserves priority 255 for in-kernel IPI ISTs

# Map Chronoscope thread states onto QNX traceprinter THREAD event names.
QNX_THREAD_EVENT = {
    "RUNNING": "THRUNNING",
    "READY": "THREADY",
    "SEND": "THSEND",
    "RECEIVE": "THRECEIVE",
    "REPLY": "THREPLY",
    "MUTEX": "THMUTEX",
    "NANOSLEEP": "THNANOSLEEP",
    "INTR": "THINTR",
    "WAITPAGE": "THWAITPAGE",
    "STOPPED": "THSTOPPED",
    "DEAD": "THDEAD",
    "SEM": "THSEM",
    "CONDVAR": "THCONDVAR",
    "JOIN": "THJOIN",
    "SIGWAITINFO": "THSIGWAITINFO",
    "SIGSUSPEND": "THSIGSUSPEND",
    "NET_SEND": "THNET_SEND",
    "NET_REPLY": "THNET_REPLY",
}


@dataclass
class TraceEvent:
    t_us: float
    kind: str
    thread: str = ""
    cpu: int = 0
    priority: int = 0
    detail: str = ""
    pid: int = 0
    tid: int = 0
    qnx_class: str = ""
    qnx_event: str = ""


@dataclass
class Segment:
    """A contiguous slice of CPU occupancy (or blocked time)."""

    start_us: float
    end_us: float
    thread: str
    lane: str          # thread name, "IDLE", "ISR", or a CPU id
    kind: str          # "run", "idle", "isr", "blocked"
    cpu: int = 0
    priority: int = 0
    partition: str = ""
    reason: str = ""

    @property
    def duration_us(self) -> float:
        return self.end_us - self.start_us


@dataclass
class JobRecord:
    thread: str
    job: str
    release_us: float
    deadline_us: float
    complete_us: float | None = None
    status: str = "PENDING"       # OK | MISS | INCOMPLETE | PENDING
    cpu_us: float = 0.0
    blocked_us: float = 0.0
    priority: int = 0
    source: str = "simulated"     # simulated | interrupt | imported
    index: int = 0

    @property
    def response_us(self) -> float | None:
        if self.complete_us is None:
            return None
        return self.complete_us - self.release_us

    @property
    def slack_us(self) -> float | None:
        r = self.response_us
        return None if r is None else self.deadline_us - self.release_us - r

    @property
    def missed(self) -> bool:
        r = self.response_us
        return r is not None and r > (self.deadline_us - self.release_us)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["response_us"] = self.response_us
        d["slack_us"] = self.slack_us
        d["missed"] = self.missed
        return d


@dataclass
class Trace:
    name: str = "trace"
    horizon_us: float = 0.0
    cpus: int = 1
    source: str = "simulated"
    events: list[TraceEvent] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    jobs: list[JobRecord] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    # -- mutation ----------------------------------------------------------
    def event(self, t_us: float, kind: str, thread: str = "", **kw: Any) -> None:
        # Defensive: every numeric column must survive JSON round-trips and
        # third-party importers that may pass strings.
        for key in ("cpu", "priority", "pid", "tid"):
            if key in kw:
                try:
                    kw[key] = int(kw[key])
                except (TypeError, ValueError):
                    kw[key] = 0
        self.events.append(TraceEvent(t_us=round(float(t_us), 3), kind=kind,
                                      thread=thread, **kw))

    def run(self, start_us: float, end_us: float, thread: str, cpu: int,
            priority: int, partition: str = "") -> None:
        if end_us <= start_us:
            return
        # Coalesce adjacent slices belonging to the same thread on the same CPU
        # so the HTML timeline stays compact for long simulations.
        if self.segments:
            last = self.segments[-1]
            if (last.kind == "run" and last.thread == thread and last.cpu == cpu
                    and abs(last.end_us - start_us) < 1e-6
                    and last.priority == priority and last.partition == partition):
                last.end_us = round(float(end_us), 3)
                return
        self.segments.append(Segment(round(float(start_us), 3), round(float(end_us), 3),
                                     thread, thread, "run", cpu, priority, partition))

    def idle(self, start_us: float, end_us: float, cpu: int) -> None:
        if end_us <= start_us:
            return
        if self.segments:
            last = self.segments[-1]
            if (last.kind == "idle" and last.cpu == cpu
                    and abs(last.end_us - start_us) < 1e-6):
                last.end_us = round(float(end_us), 3)
                return
        self.segments.append(Segment(round(float(start_us), 3), round(float(end_us), 3),
                                     "IDLE", "IDLE", "idle", cpu))

    def isr(self, start_us: float, end_us: float, label: str) -> None:
        if end_us <= start_us:
            return
        self.segments.append(Segment(round(float(start_us), 3), round(float(end_us), 3),
                                     label, "ISR", "isr", priority=PRIO_IPI_IST))

    def block(self, start_us: float, end_us: float, thread: str,
              reason: str, priority: int = 0, cpu: int = 0) -> None:
        """Record a blocking interval on the CPU the thread last ran on.

        The CPU matters on SMP targets: a lower-priority thread running on a
        *different* CPU cannot be the cause of this thread's wait.
        """
        if end_us <= start_us:
            return
        self.segments.append(Segment(round(float(start_us), 3), round(float(end_us), 3),
                                     thread, thread, "blocked", cpu,
                                     priority=priority, reason=reason))

    def sort(self) -> "Trace":
        # Python's sort is stable: sorting by time alone preserves the exact
        # causal order in which the simulator emitted same-instant events.
        self.events.sort(key=lambda e: e.t_us)
        self.segments.sort(key=lambda s: s.start_us)
        self.jobs.sort(key=lambda j: j.release_us)
        return self

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "horizon_us": self.horizon_us,
            "cpus": self.cpus,
            "source": self.source,
            "meta": self.meta,
            "events": [asdict(e) for e in self.events],
            "segments": [asdict(s) for s in self.segments],
            "jobs": [j.to_dict() for j in self.jobs],
        }

    def to_json(self, path: str | Path, indent: int = 2) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=indent) + "\n",
                              encoding="utf-8")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trace":
        t = cls(name=d.get("name", "trace"), horizon_us=d.get("horizon_us", 0.0),
                cpus=d.get("cpus", 1), source=d.get("source", "imported"),
                meta=d.get("meta", {}))
        t.events = [TraceEvent(**e) for e in d.get("events", [])]
        t.segments = [Segment(**s) for s in d.get("segments", [])]
        for j in d.get("jobs", []):
            j = {k: v for k, v in j.items()
                 if k in JobRecord.__dataclass_fields__}
            t.jobs.append(JobRecord(**j))
        return t.sort()

    @classmethod
    def from_json(cls, path: str | Path) -> "Trace":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # -- QNX-compatible text export ---------------------------------------
    def to_traceprinter_text(self) -> str:
        """Emit a stream that is structurally identical to ``traceprinter -v``.

        Useful as a golden-file bridge: the QNX target-side probe writes the
        same event vocabulary via slogger2/TraceEvent, and Chronoscope can read
        both with :mod:`chronoscope.importers`.
        """
        lines = [
            "TRACEPRINTER version 0.94 (Chronoscope-compatible export)",
            " -- HEADER FILE INFORMATION -- ",
            f"       TRACE_FILE_NAME:: {self.name}.kev",
            "        TRACE_ENCODING:: 16 byte events",
            f"         TRACE_CPU_NUM:: {self.cpus}",
            "         TRACE_SYSNAME:: QNX",
            "     TRACE_SYS_RELEASE:: 8.0",
            " -- KERNEL EVENTS -- ",
        ]
        # Emit a process-name table first, exactly like traceprinter does for
        # PROCCREATE events, so a host-side import recovers thread names.
        names: dict[int, str] = {}
        for e in self.events:
            if e.pid and e.thread and e.pid not in names:
                names[e.pid] = e.thread
        for pid, nm in sorted(names.items()):
            lines.append(f"t:0x00000000 CPU:00 PROCESS :PROCCREATE    "
                         f"name:{nm} pid:{pid} tid:0")
        for e in self.events:
            clk = int(e.t_us * 1000) & 0xFFFFFFFFFFFFFFFF
            if e.kind == "interrupt_entry":
                lines.append(f"t:0x{clk:08x} CPU:{e.cpu:02d} INT_ENTR :"
                             f" {e.detail}")
                continue
            if e.kind == "interrupt_exit":
                lines.append(f"t:0x{clk:08x} CPU:{e.cpu:02d} INT_EXIT :"
                             f" {e.detail}")
                continue
            if e.kind == "control_time":
                lines.append(f"t:0x{clk:08x} CPU:{e.cpu:02d} CONTROL :TIME "
                             f"msb:0x00000000, lsb(offset):0x{clk:08x}")
                continue
            if e.kind == "kernel_call":
                lines.append(f"t:0x{clk:08x} CPU:{e.cpu:02d} KER_CALL :"
                             f"{e.detail} pid:{e.pid} tid:{e.tid}")
                continue
            if e.kind == "kernel_exit":
                lines.append(f"t:0x{clk:08x} CPU:{e.cpu:02d} KER_EXIT :"
                             f"{e.detail} pid:{e.pid} tid:{e.tid}")
                continue
            qev = QNX_THREAD_EVENT.get(e.kind.upper())
            if qev is None:
                # Chronoscope-only annotations travel in their own class so the
                # exported stream remains a valid traceprinter-style stream.
                lines.append(f"t:0x{clk:08x} CPU:{e.cpu:02d} CHRONO  :"
                             f"{e.kind.upper():<13} {e.thread} {e.detail}".rstrip())
                continue
            suffix = f" {e.detail}" if e.detail else ""
            # The QNX-side Chronoscope probe tags THREAD events with the
            # effective priority so a host-side import can reconstruct
            # priority-inversion episodes. Real `traceprinter -v` output has
            # no such tag; the importer then falls back to a pidin snapshot.
            prio = f" prio:{e.priority}" if e.priority else ""
            lines.append(f"t:0x{clk:08x} CPU:{e.cpu:02d} THREAD  :{qev:<13}"
                         f" pid:{e.pid} tid:{e.tid}{suffix}{prio}")
        return "\n".join(lines) + "\n"


def merge_traces(traces: Iterable[Trace], name: str = "merged") -> Trace:
    """Combine several traces (e.g. baseline and fixed) into one container."""
    out = Trace(name=name)
    for t in traces:
        out.events.extend(t.events)
        out.segments.extend(t.segments)
        out.jobs.extend(t.jobs)
        out.horizon_us = max(out.horizon_us, t.horizon_us)
        out.cpus = max(out.cpus, t.cpus)
    return out.sort()
