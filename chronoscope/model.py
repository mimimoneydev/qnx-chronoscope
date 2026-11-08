"""Declarative model of a QNX Neutrino 8.0 real-time system.

Chronoscope models a QNX OS system the way a system architect describes one:
threads, priorities, scheduling policies, channels (native message passing),
mutexes, interrupt service threads, and adaptive-partition budgets.

The numbers in this module are taken from the QNX SDP 8.0 documentation:

* "Scheduling priority" -- System Architecture guide: the OS supports a total
  of 256 scheduling priority levels; 0 is idle, 1..(priority-1) is the
  unprivileged range, `procnto -P priority` sets the ceiling (63 by default),
  254 is reserved for clock ISTs and 255 for IPI ISTs.
* "Priority inheritance and messages" -- System Architecture guide: a server
  inherits the priority of the highest-priority sender, the change happens at
  *send* time when the sender is higher priority, the priority does *not*
  revert on MsgReply(), and _NTO_CHF_FIXED_PRIORITY disables inheritance.
* procmgr_ability() -- PROCMGR_AID_PRIORITY caps inheritance for a server that
  does not hold that ability.

Config files are TOML, parsed with the standard-library ``tomllib`` (Python
3.11+). No third-party dependencies are required anywhere in Chronoscope.
"""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# QNX Neutrino 8.0 constants
# ---------------------------------------------------------------------------

PRIORITY_LEVELS = 256
PRIO_IDLE = 0
PRIO_CLOCK_IST = 254
PRIO_IPI_IST = 255
DEFAULT_UNPRIVILEGED_CEILING = 63  # procnto -P default
DEFAULT_TICK_US = 1000             # 1 ms clock period
DEFAULT_RR_QUANTUM_US = 4000      # 4 x tick, QNX round-robin default
PRIVILEGED_MAX = 253
MAX_SIM_US = 20_000_000           # guard rail: 20 s of simulated time

VALID_POLICIES = ("FIFO", "RR", "OTHER", "SPORADIC")
VALID_PROTOCOLS = ("inherit", "protect", "none")
VALID_ROLES = ("worker", "server", "ist")
VALID_OPS = (
    "compute", "sleep", "send", "pulse", "lock", "unlock",
    "yield", "interrupt_wait", "set_priority",
)


class ModelError(ValueError):
    """Raised when a system model is invalid or internally inconsistent."""


@dataclass
class Step:
    """One instruction in a thread's job script."""

    op: str
    args: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, **self.args}


@dataclass
class Job:
    """A periodically released unit of work with a hard deadline."""

    name: str
    period_us: int
    deadline_us: int
    offset_us: int = 0
    steps: list[Step] = field(default_factory=list)
    release_count: int | None = None
    critical: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "period_us": self.period_us,
            "offset_us": self.offset_us,
            "deadline_us": self.deadline_us,
            "steps": [s.to_dict() for s in self.steps],
        }
        if self.release_count is not None:
            d["release_count"] = self.release_count
        if self.critical:
            d["critical"] = True
        return d


@dataclass
class ThreadSpec:
    """A QNX thread."""

    name: str
    priority: int = 10
    policy: str = "RR"
    role: str = "worker"
    partition: str = "system"
    jobs: list[Job] = field(default_factory=list)
    # server role
    channel: str | None = None
    service_us: int = 0
    fixed_priority: bool = False          # _NTO_CHF_FIXED_PRIORITY on the channel
    ability_priority: bool = False        # PROCMGR_AID_PRIORITY
    # SCHED_SPORADIC
    sporadic_budget_us: int = 0
    sporadic_period_us: int = 0
    sporadic_low_priority: int = 1
    # SCHED_RR
    quantum_us: int = DEFAULT_RR_QUANTUM_US
    # adaptive partitioning / multicore
    critical_thread: bool = False
    affinity: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "priority": self.priority,
            "policy": self.policy,
            "role": self.role,
            "partition": self.partition,
            "jobs": [j.to_dict() for j in self.jobs],
        }
        for key, default in _FIELD_DEFAULTS.items():
            val = getattr(self, key)
            # Only emit non-default values so generated models stay readable.
            if val != default:
                d[key] = val
        return d


_FIELD_DEFAULTS = {
    "channel": None,
    "service_us": 0,
    "fixed_priority": False,
    "ability_priority": False,
    "sporadic_budget_us": 0,
    "sporadic_period_us": 0,
    "sporadic_low_priority": 1,
    "quantum_us": DEFAULT_RR_QUANTUM_US,
    "critical_thread": False,
    "affinity": [],
}


@dataclass
class ChannelSpec:
    """A QNX channel created with ChannelCreate()."""

    name: str
    server: str
    fixed_priority: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "server": self.server,
            "fixed_priority": self.fixed_priority,
        }


@dataclass
class MutexSpec:
    name: str
    protocol: str = "inherit"
    ceiling: int = 0  # for PTHREAD_PRIO_PROTECT

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "protocol": self.protocol, "ceiling": self.ceiling}


@dataclass
class PartitionSpec:
    """An adaptive partitioning scheduler (APS) partition."""

    name: str
    budget_pct: float = 100.0
    critical_pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "budget_pct": self.budget_pct,
            "critical_pct": self.critical_pct,
        }


@dataclass
class InterruptSpec:
    """A hardware interrupt: ISR stub + IST thread."""

    name: str
    irq: int
    period_us: int
    isr_us: int
    ist_thread: str
    ist_wcet_us: int
    deadline_us: int
    offset_us: int = 0
    release_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "irq": self.irq,
            "period_us": self.period_us,
            "offset_us": self.offset_us,
            "isr_us": self.isr_us,
            "ist_thread": self.ist_thread,
            "ist_wcet_us": self.ist_wcet_us,
            "deadline_us": self.deadline_us,
        }
        if self.release_count is not None:
            d["release_count"] = self.release_count
        return d


@dataclass
class SystemModel:
    name: str = "qnx-system"
    description: str = ""
    horizon_us: int = 20_000
    cpus: int = 1
    tick_us: int = DEFAULT_TICK_US
    rr_quantum_us: int = DEFAULT_RR_QUANTUM_US
    unprivileged_ceiling: int = DEFAULT_UNPRIVILEGED_CEILING
    aps_enabled: bool = False
    aps_window_us: int = 100_000
    aps_free_time_by_ratio: bool = True
    threads: list[ThreadSpec] = field(default_factory=list)
    channels: list[ChannelSpec] = field(default_factory=list)
    mutexes: list[MutexSpec] = field(default_factory=list)
    partitions: list[PartitionSpec] = field(default_factory=list)
    interrupts: list[InterruptSpec] = field(default_factory=list)

    # -- lookups -----------------------------------------------------------
    def thread(self, name: str) -> ThreadSpec:
        for t in self.threads:
            if t.name == name:
                return t
        raise ModelError(f"unknown thread: {name!r}")

    def channel(self, name: str) -> ChannelSpec | None:
        for c in self.channels:
            if c.name == name:
                return c
        return None

    def clone(self) -> "SystemModel":
        return copy.deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": {
                "name": self.name,
                "description": self.description,
                "horizon_us": self.horizon_us,
                "cpus": self.cpus,
                "tick_us": self.tick_us,
                "rr_quantum_us": self.rr_quantum_us,
                "unprivileged_ceiling": self.unprivileged_ceiling,
                "aps_enabled": self.aps_enabled,
                "aps_window_us": self.aps_window_us,
                "aps_free_time_by_ratio": self.aps_free_time_by_ratio,
            },
            "threads": [t.to_dict() for t in self.threads],
            "channels": [c.to_dict() for c in self.channels],
            "mutexes": [m.to_dict() for m in self.mutexes],
            "partitions": [p.to_dict() for p in self.partitions],
            "interrupts": [i.to_dict() for i in self.interrupts],
        }


# ---------------------------------------------------------------------------
# TOML parsing
# ---------------------------------------------------------------------------

def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ModelError(message)


def _as_int(value: Any, what: str) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool),
             f"{what} must be an integer, got {value!r}")
    return int(value)


def _parse_step(raw: dict[str, Any], where: str) -> Step:
    _require(isinstance(raw, dict), f"{where}: each step must be a table")
    op = raw.get("op")
    _require(op in VALID_OPS,
             f"{where}: unknown op {op!r}; valid ops: {', '.join(VALID_OPS)}")
    args = {k: v for k, v in raw.items() if k != "op"}
    if op in ("compute", "sleep", "send", "pulse"):
        us_key = "us" if op in ("compute", "sleep") else "service_us"
        if op == "send":
            _require("channel" in args, f"{where}: send requires 'channel'")
            _require("service_us" in args, f"{where}: send requires 'service_us'")
            args["service_us"] = _as_int(args["service_us"], f"{where}.service_us")
        elif op == "pulse":
            _require("channel" in args, f"{where}: pulse requires 'channel'")
            args.setdefault("priority", None)
        else:
            _require("us" in args, f"{where}: {op} requires 'us'")
            args["us"] = _as_int(args["us"], f"{where}.us")
            _require(args["us"] >= 0, f"{where}.us must be >= 0")
    elif op in ("lock", "unlock"):
        _require("mutex" in args, f"{where}: {op} requires 'mutex'")
    elif op == "set_priority":
        _require("priority" in args, f"{where}: set_priority requires 'priority'")
        args["priority"] = _as_int(args["priority"], f"{where}.priority")
    elif op == "interrupt_wait":
        _require("irq" in args, f"{where}: interrupt_wait requires 'irq'")
    return Step(op=op, args=args)


def _parse_job(raw: dict[str, Any], where: str) -> Job:
    _require(isinstance(raw, dict), f"{where}: each job must be a table")
    name = raw.get("name", "job")
    period = _as_int(raw.get("period_us"), f"{where}.period_us")
    deadline = _as_int(raw.get("deadline_us"), f"{where}.deadline_us")
    offset = _as_int(raw.get("offset_us", 0), f"{where}.offset_us")
    _require(period > 0, f"{where}.period_us must be > 0")
    _require(deadline > 0, f"{where}.deadline_us must be > 0")
    _require(offset >= 0, f"{where}.offset_us must be >= 0")
    steps_raw = raw.get("steps", [])
    _require(isinstance(steps_raw, list), f"{where}.steps must be an array")
    steps = [_parse_step(s, f"{where}.steps[{n}]") for n, s in enumerate(steps_raw)]
    rc = raw.get("release_count")
    if rc is not None:
        rc = _as_int(rc, f"{where}.release_count")
        _require(rc > 0, f"{where}.release_count must be > 0")
    return Job(name=str(name), period_us=period, deadline_us=deadline,
               offset_us=offset, steps=steps, release_count=rc,
               critical=bool(raw.get("critical", False)))


def model_from_dict(data: dict[str, Any]) -> SystemModel:
    """Build a validated :class:`SystemModel` from plain dictionaries."""
    _require(isinstance(data, dict), "model root must be a table")
    syscfg = data.get("system", {})
    _require(isinstance(syscfg, dict), "system must be a table")

    m = SystemModel(
        name=str(syscfg.get("name", "qnx-system")),
        description=str(syscfg.get("description", "")),
        horizon_us=_as_int(syscfg.get("horizon_us", 20_000), "system.horizon_us"),
        cpus=_as_int(syscfg.get("cpus", 1), "system.cpus"),
        tick_us=_as_int(syscfg.get("tick_us", DEFAULT_TICK_US), "system.tick_us"),
        rr_quantum_us=_as_int(syscfg.get("rr_quantum_us", DEFAULT_RR_QUANTUM_US),
                              "system.rr_quantum_us"),
        unprivileged_ceiling=_as_int(
            syscfg.get("unprivileged_ceiling", DEFAULT_UNPRIVILEGED_CEILING),
            "system.unprivileged_ceiling"),
        aps_enabled=bool(syscfg.get("aps_enabled", False)),
        aps_window_us=_as_int(syscfg.get("aps_window_us", 100_000),
                              "system.aps_window_us"),
        aps_free_time_by_ratio=bool(syscfg.get("aps_free_time_by_ratio", True)),
    )
    _require(0 < m.horizon_us <= MAX_SIM_US,
             f"system.horizon_us must be in (0, {MAX_SIM_US}]")
    _require(1 <= m.cpus <= 8, "system.cpus must be in [1, 8]")
    _require(m.tick_us > 0, "system.tick_us must be > 0")
    _require(m.rr_quantum_us > 0, "system.rr_quantum_us must be > 0")
    _require(2 <= m.unprivileged_ceiling <= PRIVILEGED_MAX + 1,
             "system.unprivileged_ceiling must be in [2, 254] (procnto -P)")

    # Partitions
    parts = data.get("partitions", [])
    _require(isinstance(parts, list), "partitions must be an array")
    for n, p in enumerate(parts):
        _require(isinstance(p, dict), f"partitions[{n}] must be a table")
        budget = float(p.get("budget_pct", 100.0))
        _require(0.0 <= budget <= 100.0, f"partitions[{n}].budget_pct out of range")
        m.partitions.append(PartitionSpec(
            name=str(p.get("name", f"p{n}")), budget_pct=budget,
            critical_pct=float(p.get("critical_pct", 0.0))))
    if not m.partitions:
        m.partitions.append(PartitionSpec(name="system", budget_pct=100.0))
    pnames = [p.name for p in m.partitions]
    _require(len(pnames) == len(set(pnames)), "partition names must be unique")
    if m.aps_enabled:
        total = sum(p.budget_pct for p in m.partitions)
        _require(abs(total - 100.0) < 1e-6,
                 f"APS enabled: partition budgets must sum to 100 (got {total})")

    # Channels
    chans = data.get("channels", [])
    _require(isinstance(chans, list), "channels must be an array")
    for n, c in enumerate(chans):
        _require(isinstance(c, dict), f"channels[{n}] must be a table")
        m.channels.append(ChannelSpec(
            name=str(c.get("name", f"ch{n}")),
            server=str(c.get("server", "")),
            fixed_priority=bool(c.get("fixed_priority", False))))
    cnames = [c.name for c in m.channels]
    _require(len(cnames) == len(set(cnames)), "channel names must be unique")

    # Mutexes
    mtxs = data.get("mutexes", [])
    _require(isinstance(mtxs, list), "mutexes must be an array")
    for n, x in enumerate(mtxs):
        _require(isinstance(x, dict), f"mutexes[{n}] must be a table")
        proto = str(x.get("protocol", "inherit"))
        _require(proto in VALID_PROTOCOLS, f"mutexes[{n}].protocol invalid")
        m.mutexes.append(MutexSpec(
            name=str(x.get("name", f"m{n}")), protocol=proto,
            ceiling=_as_int(x.get("ceiling", 0), f"mutexes[{n}].ceiling")))
    mnames = [x.name for x in m.mutexes]
    _require(len(mnames) == len(set(mnames)), "mutex names must be unique")

    # Threads
    threads = data.get("threads", [])
    _require(isinstance(threads, list) and threads, "at least one thread is required")
    for n, t in enumerate(threads):
        _require(isinstance(t, dict), f"threads[{n}] must be a table")
        where = f"threads[{n}]"
        prio = _as_int(t.get("priority", 10), f"{where}.priority")
        _require(1 <= prio <= PRIVILEGED_MAX,
                 f"{where}.priority must be in [1, {PRIVILEGED_MAX}] "
                 "(0 is idle; 254/255 are reserved for in-kernel ISTs)")
        policy = str(t.get("policy", "RR")).upper()
        _require(policy in VALID_POLICIES, f"{where}.policy invalid: {policy}")
        role = str(t.get("role", "worker"))
        _require(role in VALID_ROLES, f"{where}.role invalid: {role}")
        part = str(t.get("partition", "system"))
        _require(part in pnames, f"{where}.partition {part!r} not defined")
        jobs_raw = t.get("jobs", [])
        _require(isinstance(jobs_raw, list), f"{where}.jobs must be an array")
        jobs = [_parse_job(j, f"{where}.jobs[{k}]") for k, j in enumerate(jobs_raw)]
        affinity = t.get("affinity", [])
        _require(isinstance(affinity, list), f"{where}.affinity must be an array")
        affinity = [_as_int(a, f"{where}.affinity[]") for a in affinity]
        _require(all(0 <= a < m.cpus for a in affinity),
                 f"{where}.affinity contains an invalid CPU index")
        th = ThreadSpec(
            name=str(t.get("name", f"t{n}")),
            priority=prio, policy=policy, role=role, partition=part, jobs=jobs,
            channel=t.get("channel"),
            service_us=_as_int(t.get("service_us", 0), f"{where}.service_us"),
            fixed_priority=bool(t.get("fixed_priority", False)),
            ability_priority=bool(t.get("ability_priority", False)),
            sporadic_budget_us=_as_int(t.get("sporadic_budget_us", 0),
                                       f"{where}.sporadic_budget_us"),
            sporadic_period_us=_as_int(t.get("sporadic_period_us", 0),
                                       f"{where}.sporadic_period_us"),
            sporadic_low_priority=_as_int(t.get("sporadic_low_priority", 1),
                                          f"{where}.sporadic_low_priority"),
            quantum_us=_as_int(t.get("quantum_us", m.rr_quantum_us),
                               f"{where}.quantum_us"),
            critical_thread=bool(t.get("critical_thread", False)),
            affinity=affinity,
        )
        _require(th.service_us >= 0, f"{where}.service_us must be >= 0")
        _require(th.quantum_us > 0, f"{where}.quantum_us must be > 0")
        if th.role == "server":
            _require(bool(th.channel), f"{where}: a server thread needs 'channel'")
        if th.policy == "SPORADIC":
            _require(th.sporadic_budget_us > 0 and th.sporadic_period_us > 0,
                     f"{where}: SCHED_SPORADIC needs budget and period")
            _require(1 <= th.sporadic_low_priority < th.priority,
                     f"{where}: sporadic low priority must be below normal priority")
        m.threads.append(th)
    tnames = [t.name for t in m.threads]
    _require(len(tnames) == len(set(tnames)), "thread names must be unique")

    # Channel / step references
    for th in m.threads:
        if th.channel:
            _require(th.channel in cnames,
                     f"thread {th.name}: unknown channel {th.channel!r}")
        for job in th.jobs:
            for st in job.steps:
                if st.op in ("send", "pulse"):
                    _require(st.args.get("channel") in cnames,
                             f"thread {th.name} job {job.name}: unknown channel "
                             f"{st.args.get('channel')!r}")
                if st.op in ("lock", "unlock"):
                    _require(st.args.get("mutex") in mnames,
                             f"thread {th.name} job {job.name}: unknown mutex "
                             f"{st.args.get('mutex')!r}")
    # Every channel that receives messages must have a server.
    used = set()
    for th in m.threads:
        for job in th.jobs:
            for st in job.steps:
                if st.op in ("send", "pulse"):
                    used.add(st.args.get("channel"))
    servers = {t.channel for t in m.threads if t.role == "server"}
    for c in m.channels:
        _require(c.server in tnames,
                 f"channel {c.name}: server {c.server!r} is not a thread")
        if c.name in used:
            _require(c.name in servers,
                     f"channel {c.name} receives messages but no server thread "
                     "declares it (set role='server' and channel=...)")
    # Synchronise the channel's fixed-priority flag onto its server thread.
    for c in m.channels:
        if c.fixed_priority:
            for t in m.threads:
                if t.channel == c.name:
                    t.fixed_priority = True

    # Interrupts
    ints = data.get("interrupts", [])
    _require(isinstance(ints, list), "interrupts must be an array")
    for n, i in enumerate(ints):
        _require(isinstance(i, dict), f"interrupts[{n}] must be a table")
        where = f"interrupts[{n}]"
        ist = str(i.get("ist_thread", ""))
        _require(ist in tnames, f"{where}.ist_thread {ist!r} is not a thread")
        rc = i.get("release_count")
        if rc is not None:
            rc = _as_int(rc, f"{where}.release_count")
        m.interrupts.append(InterruptSpec(
            name=str(i.get("name", f"irq{n}")),
            irq=_as_int(i.get("irq"), f"{where}.irq"),
            period_us=_as_int(i.get("period_us"), f"{where}.period_us"),
            isr_us=_as_int(i.get("isr_us", 0), f"{where}.isr_us"),
            ist_thread=ist,
            ist_wcet_us=_as_int(i.get("ist_wcet_us"), f"{where}.ist_wcet_us"),
            deadline_us=_as_int(i.get("deadline_us"), f"{where}.deadline_us"),
            offset_us=_as_int(i.get("offset_us", 0), f"{where}.offset_us"),
            release_count=rc,
        ))
        _require(m.interrupts[-1].period_us > 0, f"{where}.period_us must be > 0")
        _require(m.interrupts[-1].ist_wcet_us >= 0, f"{where}.ist_wcet_us >= 0")
    return m


def load_model(path: str | Path) -> SystemModel:
    """Load and validate a TOML system model."""
    p = Path(path)
    _require(p.is_file(), f"model file not found: {p}")
    with p.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ModelError(f"{p}: invalid TOML: {exc}") from exc
    return model_from_dict(data)


# ---------------------------------------------------------------------------
# TOML writing (tomllib is read-only, so we emit canonical TOML ourselves)
# ---------------------------------------------------------------------------

def _toml_scalar(v: Any) -> str:
    if v is None:
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_scalar(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_toml_scalar(x)}" for k, x in v.items()) + " }"
    raise ModelError(f"cannot serialize {type(v).__name__} to TOML")


def dump_model(model: SystemModel, path: str | Path) -> None:
    """Write a canonical TOML model that round-trips through :func:`load_model`."""
    d = model.to_dict()
    lines: list[str] = ["# Generated by Chronoscope for QNX 8.0",
                        "# https://github.com/mimimoneydev/qnx-chronoscope", ""]
    lines.append("[system]")
    for k, v in d["system"].items():
        lines.append(f"{k} = {_toml_scalar(v)}")
    lines.append("")

    for p in d["partitions"]:
        lines.append("[[partitions]]")
        for k, v in p.items():
            lines.append(f"{k} = {_toml_scalar(v)}")
        lines.append("")
    for c in d["channels"]:
        lines.append("[[channels]]")
        for k, v in c.items():
            lines.append(f"{k} = {_toml_scalar(v)}")
        lines.append("")
    for x in d["mutexes"]:
        lines.append("[[mutexes]]")
        for k, v in x.items():
            lines.append(f"{k} = {_toml_scalar(v)}")
        lines.append("")
    for t in d["threads"]:
        lines.append("[[threads]]")
        for k, v in t.items():
            if k == "jobs":
                continue
            lines.append(f"{k} = {_toml_scalar(v)}")
        if t["jobs"]:
            lines.append("jobs = [")
            for j in t["jobs"]:
                steps = j.pop("steps")  # noqa: PLW2901 - local copy, not reused
                head = ", ".join(f"{k} = {_toml_scalar(v)}" for k, v in j.items())
                body = ", ".join(_toml_scalar(s) for s in steps)
                lines.append(f"    {{ {head}, steps = [{body}] }},")
            lines.append("]")
        else:
            lines.append("jobs = []")
        lines.append("")
    for i in d["interrupts"]:
        lines.append("[[interrupts]]")
        for k, v in i.items():
            lines.append(f"{k} = {_toml_scalar(v)}")
        lines.append("")
    Path(path).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


EXAMPLE_MODEL = '''\
# Chronoscope for QNX 8.0 -- minimal example
# Docs: https://www.qnx.com/developers/docs/8.0/
[system]
name = "hello-qnx"
description = "Two periodic threads and a message-passing server"
horizon_us = 12000
cpus = 1
tick_us = 1000
rr_quantum_us = 4000
unprivileged_ceiling = 63   # procnto -P (QNX 8.0 default)
aps_enabled = false

[[partitions]]
name = "system"
budget_pct = 100.0

[[channels]]
name = "actuator"
server = "actuator_server"
fixed_priority = false      # _NTO_CHF_FIXED_PRIORITY

[[threads]]
name = "control"
priority = 40
policy = "FIFO"
role = "worker"
partition = "system"
jobs = [
    { name = "cycle", period_us = 2000, offset_us = 0, deadline_us = 1800,
      steps = [
          { op = "compute", us = 200 },
          { op = "send", channel = "actuator", service_us = 300 },
      ] },
]

[[threads]]
name = "telemetry"
priority = 12
policy = "RR"
role = "worker"
partition = "system"
jobs = [
    { name = "report", period_us = 5000, offset_us = 100, deadline_us = 4500,
      steps = [ { op = "compute", us = 500 } ] },
]

[[threads]]
name = "actuator_server"
priority = 20
policy = "FIFO"
role = "server"
partition = "system"
channel = "actuator"
service_us = 300
ability_priority = false
jobs = []
'''
