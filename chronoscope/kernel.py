"""Discrete-event simulator of the QNX Neutrino 8.0 microkernel scheduler.

This is not a queueing approximation. Chronoscope implements the scheduling
rules that QNX SDP 8.0 documents, so a model can be reasoned about with the
same vocabulary an embedded engineer uses on a real target:

* 256 scheduling priority levels; 0 idle, 1..(P-1) unprivileged (``procnto -P``,
  default P=63), P..253 privileged with PROCMGR_AID_PRIORITY, 254 clock ISTs,
  255 IPI ISTs.  (System Architecture > Scheduling priority)
* SCHED_FIFO, SCHED_RR, SCHED_OTHER and SCHED_SPORADIC (budget + low priority +
  replenishment).
* Native synchronous message passing: STATE_SEND / STATE_REPLY /
  STATE_RECEIVE, priority-ordered receive queues, pulses (non-blocking,
  priority-carrying), MsgReply().
* **Message-driven priority inheritance**: a server inherits the priority of
  the highest-priority sender; the boost happens at *send* time when the sender
  is higher priority; the priority does **not** revert on MsgReply(); it is
  lowered only when a lower-priority message/pulse is received, or set
  explicitly.  _NTO_CHF_FIXED_PRIORITY turns it off.  A server without
  PROCMGR_AID_PRIORITY is clamped to the highest unprivileged priority.
  (System Architecture > Priority inheritance and messages)
* Mutex priority inheritance (PTHREAD_PRIO_INHERIT) and priority protect.
* Interrupt stubs (non-preemptible) plus Interrupt Service Threads.
* Adaptive partitioning (APS): percentage budgets over a rolling averaging
  window, free-time-by-ratio borrowing, critical budget and bankruptcy.
* SMP with per-thread runmasks and preemption of the lowest-priority CPU.

The output is a :class:`~chronoscope.trace.Trace` in the same event vocabulary
that the QNX-side probe and ``traceprinter`` use.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any

from .model import (
    PRIO_IPI_IST, PRIVILEGED_MAX, SystemModel, ThreadSpec,
)
from .trace import JobRecord, Trace

IDLE_STATES = {
    "worker": "NANOSLEEP",
    "server": "RECEIVE",
    "ist": "INTR",
}
RR_POLICIES = ("RR", "OTHER")


@dataclass
class Message:
    """A QNX message or pulse sitting on a channel."""

    client: "ThreadRT | None"
    priority: int
    service_us: float
    is_pulse: bool = False
    arrival_us: float = 0.0
    seq: int = 0


@dataclass
class ChannelRT:
    name: str
    server: str
    fixed_priority: bool
    queue: list[Message] = field(default_factory=list)


@dataclass
class MutexRT:
    name: str
    protocol: str
    ceiling: int
    owner: "ThreadRT | None" = None
    waiters: list["ThreadRT"] = field(default_factory=list)


@dataclass
class ThreadRT:
    spec: ThreadSpec
    state: str = "NANOSLEEP"
    base_priority: int = 10
    sticky_priority: int = 10
    effective_priority: int = 10
    cpu: int | None = None
    ready_seq: int = 0
    remaining_us: float = 0.0
    pc: int = 0
    active_job: JobRecord | None = None
    pending_jobs: list[tuple[Any, int]] = field(default_factory=list)
    blocked_since: float = 0.0
    blocked_reason: str = ""
    blocked_cpu: int = 0      # CPU the thread last ran on before blocking
    rr_remaining_us: float = 0.0
    held_mutexes: set[str] = field(default_factory=set)
    waiting_mutex: str | None = None
    sporadic_left_us: float = 0.0
    sporadic_replenish_at: float | None = None
    # currently executing job script
    job_spec: Any = None
    job_steps: list[Any] | None = None
    wait_irq: int | None = None
    # server bookkeeping
    current_client: "ThreadRT | None" = None
    current_service_us: float = 0.0
    pid: int = 0
    tid: int = 0


@dataclass
class AdhocJob:
    """A non-periodic job (used for Interrupt Service Threads)."""

    name: str
    deadline_us: int
    steps: list[Any]


class SimulationError(RuntimeError):
    """Raised if the simulator detects an impossible internal state."""


class Kernel:
    """A deterministic, reproducibility-first QNX Neutrino scheduler model."""

    def __init__(self, model: SystemModel, collect_events: bool = True) -> None:
        self.m = model
        self.collect_events = collect_events
        self.now: float = 0.0
        self._seq = 0
        self._heap: list[tuple[float, int, str, Any]] = []
        self._trace = Trace(name=model.name, horizon_us=float(model.horizon_us),
                            cpus=model.cpus, source="simulated",
                            meta={"qnx_version": "8.0", "tool": "chronoscope",
                                  "unprivileged_ceiling": model.unprivileged_ceiling,
                                  "aps_enabled": model.aps_enabled,
                                  "cpus": model.cpus})
        self.threads: dict[str, ThreadRT] = {}
        self.channels: dict[str, ChannelRT] = {}
        self.mutexes: dict[str, MutexRT] = {}
        self.cpu_running: list[ThreadRT | None] = [None] * model.cpus
        self.aps_used: dict[str, float] = {p.name: 0.0 for p in model.partitions}
        self.aps_critical_used: dict[str, float] = {
            p.name: 0.0 for p in model.partitions}
        self.aps_budget_us: dict[str, float] = {
            p.name: model.aps_window_us * p.budget_pct / 100.0
            for p in model.partitions}
        self.aps_critical_budget_us: dict[str, float] = {
            p.name: model.aps_window_us * p.critical_pct / 100.0
            for p in model.partitions}
        self.bankruptcy_events: list[dict[str, Any]] = []
        self.free_time_us: float = 0.0
        self._iterations = 0
        self._max_iterations = 2_000_000
        self._init_objects()

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _push(self, t: float, kind: str, payload: Any = None) -> None:
        heapq.heappush(self._heap, (float(t), self._next_seq(), kind, payload))

    def _init_objects(self) -> None:
        for i, t in enumerate(self.m.threads, start=1):
            rt = ThreadRT(
                spec=t, state=IDLE_STATES[t.role],
                base_priority=t.priority, sticky_priority=t.priority,
                effective_priority=t.priority,
                sporadic_left_us=float(t.sporadic_budget_us),
                rr_remaining_us=float(t.quantum_us),
                pid=100 + i, tid=i,
            )
            rt.ready_seq = self._next_seq()
            self.threads[t.name] = rt
            self._emit(0.0, rt.state, t.name, cpu=0,
                       priority=t.priority, pid=rt.pid, tid=rt.tid,
                       detail="thread created")
        for c in self.m.channels:
            self.channels[c.name] = ChannelRT(c.name, c.server, c.fixed_priority)
        for x in self.m.mutexes:
            self.mutexes[x.name] = MutexRT(x.name, x.protocol, x.ceiling)

        # Periodic job releases -------------------------------------------------
        horizon = self.m.horizon_us
        for t in self.m.threads:
            for j in t.jobs:
                count = j.release_count
                k = 0
                while True:
                    rel = j.offset_us + k * j.period_us
                    if rel >= horizon or (count is not None and k >= count):
                        break
                    self._push(rel, "job_release", (t.name, j, k))
                    k += 1
                    if k > 200_000:
                        raise SimulationError("too many job releases; shorten horizon")
        # Hardware interrupts ---------------------------------------------------
        for irq in self.m.interrupts:
            k = 0
            while True:
                rel = irq.offset_us + k * irq.period_us
                if rel >= horizon or (irq.release_count is not None
                                      and k >= irq.release_count):
                    break
                self._push(rel, "irq", (irq, k))
                k += 1
                if k > 200_000:
                    raise SimulationError("too many interrupts; shorten horizon")
        # APS averaging windows -------------------------------------------------
        if self.m.aps_enabled:
            w = self.m.aps_window_us
            t = w
            while t < horizon:
                self._push(t, "aps_window", None)
                t += w
        self._push(0.0, "control_time", None)
        self._push(float(horizon), "end", None)

    # ------------------------------------------------------------------
    # tracing helpers
    # ------------------------------------------------------------------
    def _emit(self, t: float, kind: str, thread: str = "", **kw: Any) -> None:
        if self.collect_events:
            self._trace.event(t, kind, thread, **kw)

    def _state_event(self, th: ThreadRT, state: str, cpu: int = 0,
                     detail: str = "") -> None:
        self._emit(self.now, state, th.spec.name, cpu=cpu,
                   priority=th.effective_priority, pid=th.pid, tid=th.tid,
                   detail=detail)

    def _set_blocked(self, th: ThreadRT, state: str, reason: str) -> None:
        th.blocked_cpu = th.cpu if th.cpu is not None else 0
        th.state = state
        th.cpu = None
        th.blocked_since = self.now
        th.blocked_reason = reason
        self._state_event(th, state, cpu=th.blocked_cpu, detail=reason)

    def _wake(self, th: ThreadRT, reason: str = "") -> None:
        if th.state in ("READY", "RUNNING"):
            return
        if th.blocked_since < self.now:
            self._trace.block(th.blocked_since, self.now, th.spec.name,
                              th.blocked_reason or "BLOCKED",
                              th.effective_priority, cpu=th.blocked_cpu)
        th.state = "READY"
        th.cpu = None
        th.ready_seq = self._next_seq()
        self._state_event(th, "READY", detail=reason or "unblocked")

    # ------------------------------------------------------------------
    # priority computation (QNX message + mutex inheritance)
    # ------------------------------------------------------------------
    def _inheritance_cap(self, server: ThreadRT) -> int:
        if server.spec.ability_priority:
            return PRIVILEGED_MAX
        # "boosted to the highest unprivileged priority" -- levels 1..P-1.
        return self.m.unprivileged_ceiling - 1

    def _boost(self, server: ThreadRT, priority: int) -> None:
        """Priority inheritance at *send* time (QNX message-driven PI)."""
        ch = self.channels.get(server.spec.channel or "")
        if (ch and ch.fixed_priority) or server.spec.fixed_priority:
            return  # _NTO_CHF_FIXED_PRIORITY
        p = min(priority, self._inheritance_cap(server))
        if p > server.sticky_priority:
            server.sticky_priority = p
            self._emit(self.now, "priority_boost", server.spec.name,
                       priority=p, detail=f"message-driven PI -> {p}")
            self._recompute_priorities()

    def _lower_on_receive(self, server: ThreadRT, priority: int) -> None:
        """QNX lowers server priority when a lower-priority msg/pulse arrives."""
        ch = self.channels.get(server.spec.channel or "")
        if (ch and ch.fixed_priority) or server.spec.fixed_priority:
            return
        p = min(priority, self._inheritance_cap(server))
        if p != server.sticky_priority:
            server.sticky_priority = p
            self._emit(self.now, "priority_change", server.spec.name,
                       priority=p, detail="priority of received message")
            self._recompute_priorities()

    def _recompute_priorities(self) -> None:
        """effective = sticky, boosted by mutex waiters / PRIO_PROTECT,
        or dropped to the low priority when a SCHED_SPORADIC budget is gone."""
        boosts: dict[str, int] = {}
        for m in self.mutexes.values():
            if m.owner is None:
                continue
            if m.protocol == "protect" and m.ceiling:
                boosts[m.owner.spec.name] = max(
                    boosts.get(m.owner.spec.name, 0), m.ceiling)
            elif m.protocol == "inherit":
                for w in m.waiters:
                    boosts[m.owner.spec.name] = max(
                        boosts.get(m.owner.spec.name, 0), w.sticky_priority)
        for th in self.threads.values():
            eff = th.sticky_priority
            eff = max(eff, boosts.get(th.spec.name, 0))
            if th.spec.policy == "SPORADIC" and th.sporadic_left_us <= 0:
                eff = min(eff, th.spec.sporadic_low_priority)
            th.effective_priority = max(1, min(eff, PRIO_IPI_IST))

    # ------------------------------------------------------------------
    # APS
    # ------------------------------------------------------------------
    def _aps_remaining(self, th: ThreadRT, cpu: int) -> float:
        if not self.m.aps_enabled:
            return float("inf")
        part = th.spec.partition
        total = self.aps_budget_us[part]
        left = total - self.aps_used[part]
        if left > 0:
            return left
        if th.spec.critical_thread:
            c = self.aps_critical_budget_us[part] - self.aps_critical_used[part]
            if c > 0:
                return c
        if self.m.aps_free_time_by_ratio:
            # "Free time mode": if no other partition with budget left has a
            # READY thread, a bankrupt partition may borrow the idle CPU.
            contender = any(
                p != part
                and self.aps_budget_us[p] - self.aps_used[p] > 0
                and any(x.state in ("READY", "RUNNING")
                        and x.spec.partition == p for x in self.threads.values())
                for p in self.aps_budget_us
            )
            if not contender:
                return float("inf")
        return 0.0

    def _aps_account(self, th: ThreadRT, dt: float) -> None:
        if not self.m.aps_enabled or dt <= 0:
            return
        part = th.spec.partition
        normal = self.aps_budget_us[part] - self.aps_used[part]
        if normal > 0:
            used = min(dt, normal)
            self.aps_used[part] += used
            if dt > used and th.spec.critical_thread:
                self.aps_critical_used[part] += dt - used
        elif th.spec.critical_thread:
            self.aps_critical_used[part] += dt
        else:
            self.free_time_us += dt

    # ------------------------------------------------------------------
    # scheduler / dispatch
    # ------------------------------------------------------------------
    def _eligible(self, th: ThreadRT, cpu: int) -> bool:
        if th.spec.affinity and cpu not in th.spec.affinity:
            return False
        return True

    def _dispatch(self) -> None:
        """Choose the highest-priority READY thread for each CPU (QNX SMP rule)."""
        for th in self.threads.values():
            if th.state == "RUNNING":
                th.state = "READY"  # will be re-assigned below if still chosen
                th.cpu = None
        pool = [th for th in self.threads.values() if th.state == "READY"]
        # Higher priority first; ties by queue position. A preempted thread
        # keeps its old (smaller) ready_seq and therefore returns to the HEAD
        # of its priority queue, matching QNX behaviour.
        pool.sort(key=lambda t: (-t.effective_priority, t.ready_seq))
        assigned: list[ThreadRT | None] = [None] * self.m.cpus
        used: set[str] = set()
        for cpu in range(self.m.cpus):
            chosen = None
            for th in pool:
                if th.spec.name in used or not self._eligible(th, cpu):
                    continue
                if self._aps_remaining(th, cpu) <= 0:
                    continue
                chosen = th
                break
            if chosen is not None:
                used.add(chosen.spec.name)
                assigned[cpu] = chosen
        for cpu, th in enumerate(assigned):
            if th is None:
                continue
            was_running = self.cpu_running[cpu] is th
            th.state = "RUNNING"
            th.cpu = cpu
            if not was_running:
                th.rr_remaining_us = float(th.spec.quantum_us)
                self._state_event(th, "RUNNING", cpu=cpu, detail="dispatched")
        for th in pool:
            if th.state == "READY" and th.spec.name not in used:
                # stayed / became READY but not dispatched
                pass
        self.cpu_running = assigned

    # ------------------------------------------------------------------
    # job / step execution
    # ------------------------------------------------------------------
    def _start_job(self, th: ThreadRT, job: Any, index: int,
                   release: float | None = None,
                   deadline_us: float | None = None,
                   source: str = "simulated",
                   steps_override: list[Any] | None = None) -> None:
        rel = self.now if release is None else release
        dl = rel + (job.deadline_us if deadline_us is None else deadline_us)
        rec = JobRecord(thread=th.spec.name, job=job.name,
                        release_us=rel, deadline_us=dl, priority=th.base_priority,
                        source=source, index=index)
        th.active_job = rec
        th.pc = 0
        th.job_spec = job
        th.job_steps = steps_override if steps_override is not None else job.steps
        self._trace.jobs.append(rec)
        self._emit(rel, "job_release", th.spec.name, priority=th.effective_priority,
                   detail=f"{rec.job}#{index} deadline={dl:.0f}us")
        self._execute(th)

    def _steps(self, th: ThreadRT) -> list[Any]:
        return th.job_steps if th.job_steps is not None else []

    def _complete_job(self, th: ThreadRT) -> None:
        rec = th.active_job
        if rec is not None:
            rec.complete_us = round(self.now, 3)
            rec.status = "MISS" if rec.missed else "OK"
            self._emit(self.now, "job_complete", th.spec.name,
                       detail=f"{rec.job}#{rec.index} status={rec.status} "
                              f"response={rec.response_us:.0f}us")
            if rec.missed:
                self._emit(self.now, "deadline_miss", th.spec.name,
                           detail=f"{rec.job}#{rec.index} "
                                  f"late by {-rec.slack_us:.0f}us")
        th.active_job = None
        th.job_steps = None
        th.job_spec = None

    def _execute(self, th: ThreadRT) -> None:
        """Run non-blocking steps until CPU work is needed or the thread blocks."""
        for _ in range(1000):
            if th.active_job is None:
                if th.pending_jobs:
                    job, index = th.pending_jobs.pop(0)
                    src = "interrupt" if isinstance(job, AdhocJob) else "simulated"
                    self._start_job(th, job, index, source=src)
                    return
                self._set_blocked(th, IDLE_STATES[th.spec.role],
                                  f"waiting for {th.spec.role} work")
                return
            steps = self._steps(th)
            if th.pc >= len(steps):
                self._complete_job(th)
                continue
            step = steps[th.pc]
            op = step.op
            a = step.args
            if op == "compute":
                th.remaining_us = float(a["us"])
                if th.remaining_us <= 0:
                    th.pc += 1
                    continue
                if th.state not in ("READY", "RUNNING"):
                    th.state = "READY"
                    th.ready_seq = self._next_seq()
                return
            if op == "sleep":
                us = float(a["us"])
                self._set_blocked(th, "NANOSLEEP", f"nanosleep({us:.0f}us)")
                if us <= 0:
                    th.pc += 1
                    self._wake(th)
                    continue
                self._push(self.now + us, "wake", (th, "sleep"))
                return
            if op == "send":
                self._do_send(th, a)
                return
            if op == "pulse":
                self._do_pulse(th, a)
                th.pc += 1
                continue
            if op == "lock":
                if self._do_lock(th, a["mutex"]):
                    th.pc += 1
                    continue
                return
            if op == "unlock":
                self._do_unlock(th, a["mutex"])
                th.pc += 1
                continue
            if op == "yield":
                # sched_yield(): move to the tail of this priority queue.
                th.ready_seq = self._next_seq()
                th.pc += 1
                continue
            if op == "set_priority":
                p = int(a["priority"])
                th.base_priority = p
                th.sticky_priority = p
                self._emit(self.now, "priority_change", th.spec.name, priority=p,
                           detail="pthread_setschedprio()")
                self._recompute_priorities()
                th.pc += 1
                continue
            if op == "interrupt_wait":
                self._set_blocked(th, "INTR", f"InterruptWait(irq={a['irq']})")
                th.wait_irq = int(a["irq"])
                return
            raise SimulationError(f"unhandled step op: {op}")
        raise SimulationError("step execution did not converge")

    # ------------------------------------------------------------------
    # native message passing
    # ------------------------------------------------------------------
    def _do_send(self, th: ThreadRT, a: dict[str, Any]) -> None:
        ch = self.channels[a["channel"]]
        service = float(a.get("service_us", 0))
        msg = Message(client=th, priority=th.effective_priority,
                      service_us=service, arrival_us=self.now,
                      seq=self._next_seq())
        self._emit(self.now, "kernel_call", th.spec.name,
                   pid=th.pid, tid=th.tid, detail=f"MsgSend({ch.name})")
        self._set_blocked(th, "SEND", f"STATE_SEND on {ch.name}")
        ch.queue.append(msg)
        server = self.threads[ch.server]
        self._boost(server, msg.priority)  # inheritance happens at SEND time
        self._try_deliver(ch)

    def _do_pulse(self, th: ThreadRT, a: dict[str, Any]) -> None:
        ch = self.channels[a["channel"]]
        p = a.get("priority")
        prio = th.effective_priority if p in (None, "") else int(p)
        service = float(a.get("service_us", 0))
        msg = Message(client=None, priority=prio, service_us=service,
                      is_pulse=True, arrival_us=self.now, seq=self._next_seq())
        self._emit(self.now, "kernel_call", th.spec.name,
                   pid=th.pid, tid=th.tid, detail=f"MsgSendPulse({ch.name})")
        ch.queue.append(msg)
        server = self.threads[ch.server]
        self._boost(server, prio)
        self._try_deliver(ch)

    def _try_deliver(self, ch: ChannelRT) -> None:
        server = self.threads[ch.server]
        if not ch.queue or server.state not in (IDLE_STATES["server"],):
            return
        # Server receives messages/pulses in priority order.
        ch.queue.sort(key=lambda m: (-m.priority, m.arrival_us, m.seq))
        msg = ch.queue.pop(0)
        self._lower_on_receive(server, msg.priority)
        self._emit(self.now, "kernel_call", server.spec.name,
                   pid=server.pid, tid=server.tid,
                   detail=f"MsgReceive({ch.name})")
        server.current_client = msg.client
        server.current_service_us = (msg.service_us if msg.service_us > 0
                                     else float(server.spec.service_us))
        if msg.client is not None and msg.client.state == "SEND":
            # Client is now REPLY-blocked (QNX: skipped STATE_SEND when a
            # receiver is already waiting, otherwise transitioned here).
            msg.client.blocked_reason = f"STATE_REPLY on {ch.name}"
            msg.client.state = "REPLY"
            self._state_event(msg.client, "REPLY",
                              detail=f"received by {server.spec.name}")
        if server.current_service_us > 0:
            server.remaining_us = server.current_service_us
            self._wake(server, f"MsgReceive({ch.name})")
        else:
            self._finish_server_message(server)

    def _finish_server_message(self, server: ThreadRT) -> None:
        client = server.current_client
        if client is not None:
            self._emit(self.now, "kernel_call", server.spec.name,
                       pid=server.pid, tid=server.tid,
                       detail=f"MsgReply({client.spec.name})")
            # QNX: the server priority does NOT revert when it replies.
            self._wake(client, "MsgReply()")
            self._state_event(client, "READY", detail="reply received")
            # Advance the client past its MsgSend step.
            client.pc += 1
            self._execute(client)
        server.current_client = None
        server.current_service_us = 0.0
        server.remaining_us = 0.0
        # Return to MsgReceive() *before* attempting another delivery; QNX
        # only delivers to a RECEIVE-blocked thread.
        self._set_blocked(server, "RECEIVE",
                          f"MsgReceive() on {server.spec.channel}")
        ch = self.channels.get(server.spec.channel or "")
        if ch and ch.queue:
            self._try_deliver(ch)

    # ------------------------------------------------------------------
    # mutexes
    # ------------------------------------------------------------------
    def _do_lock(self, th: ThreadRT, name: str) -> bool:
        m = self.mutexes[name]
        if m.owner is None:
            m.owner = th
            th.held_mutexes.add(name)
            return True
        self._set_blocked(th, "MUTEX", f"pthread_mutex_lock({name})")
        th.waiting_mutex = name
        m.waiters.append(th)
        self._recompute_priorities()
        return False

    def _do_unlock(self, th: ThreadRT, name: str) -> None:
        m = self.mutexes[name]
        if m.owner is not th:
            raise SimulationError(f"{th.spec.name} unlocks unowned mutex {name}")
        m.owner = None
        th.held_mutexes.discard(name)
        if m.waiters:
            m.waiters.sort(key=lambda w: (-w.effective_priority, w.ready_seq))
            nxt = m.waiters.pop(0)
            m.owner = nxt
            nxt.held_mutexes.add(name)
            nxt.waiting_mutex = None
            nxt.pc += 1  # the lock step completes when the mutex is acquired
            self._wake(nxt, f"acquired {name}")
            self._execute(nxt)
        self._recompute_priorities()

    # ------------------------------------------------------------------
    # event handlers
    # ------------------------------------------------------------------
    def _on_job_release(self, thread_name: str, job: Any, index: int) -> None:
        th = self.threads[thread_name]
        self._emit(self.now, "job_release_scheduled", thread_name,
                   detail=f"{job.name}#{index}")
        th.pending_jobs.append((job, index))
        if th.state in tuple(IDLE_STATES.values()):
            self._wake(th, f"timer pulse releases {job.name}")
            self._execute(th)
        # If the thread is blocked, it will pick the queued job up as soon as
        # it unblocks (see _execute()).

    def _on_irq(self, irq: Any, index: int) -> None:
        # The ISR stub is non-preemptible: on a single-core target nothing else
        # runs while it executes. We preserve each running thread's remaining
        # budget and return it to READY afterwards.
        self._emit(self.now, "interrupt_entry", "ISR", priority=PRIO_IPI_IST,
                   detail=f"irq={irq.irq} hex=0x{irq.irq:x}")
        for cpu, th in enumerate(self.cpu_running):
            if th is not None and th.state == "RUNNING":
                th.state = "READY"
                th.cpu = None
        self.cpu_running = [None] * self.m.cpus
        if irq.isr_us > 0:
            self._trace.isr(self.now, self.now + irq.isr_us,
                            f"ISR irq={irq.irq}")
            self.now += irq.isr_us
        self._emit(self.now, "interrupt_exit", "ISR", priority=PRIO_IPI_IST,
                   detail=f"irq={irq.irq}")
        # Wake ISTs blocked in InterruptWait().
        for th in self.threads.values():
            if th.state == "INTR" and th.wait_irq == irq.irq:
                th.wait_irq = None
                th.pc += 1
                self._wake(th, f"InterruptWait() irq={irq.irq}")
                self._execute(th)
        ist = self.threads[irq.ist_thread]
        from .model import Step
        synthetic = [Step(op="compute", args={"us": irq.ist_wcet_us})]
        ist_job = AdhocJob(name=f"{irq.name}_ist",
                           deadline_us=irq.deadline_us, steps=synthetic)
        ist.pending_jobs.append((ist_job, index))
        if ist.state in tuple(IDLE_STATES.values()):
            self._wake(ist, f"IST released for irq {irq.irq}")
            self._execute(ist)

    def _on_aps_window(self) -> None:
        for p in self.m.partitions:
            if (self.aps_used[p.name] >= self.aps_budget_us[p.name] - 1e-9
                    and self.aps_budget_us[p.name] > 0):
                self.bankruptcy_events.append({
                    "t_us": self.now, "partition": p.name,
                    "used_us": round(self.aps_used[p.name], 3),
                    "budget_us": round(self.aps_budget_us[p.name], 3),
                })
                self._emit(self.now, "aps_bankruptcy", "",
                           detail=f"partition {p.name} exhausted budget")
            self.aps_used[p.name] = 0.0
            self.aps_critical_used[p.name] = 0.0

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def run(self) -> Trace:
        """Simulate ``horizon_us`` of QNX OS time and return the trace."""
        self._recompute_priorities()
        self._process_events_at(self.now)
        self._dispatch()

        while self._heap and self.now < self.m.horizon_us:
            self._iterations += 1
            if self._iterations > self._max_iterations:
                raise SimulationError("simulation did not converge")

            next_t = self._heap[0][0] if self._heap else self.m.horizon_us
            limits: list[float] = [next_t - self.now]
            running = [t for t in self.cpu_running if t is not None]
            for th in running:
                if th.remaining_us > 0:
                    limits.append(th.remaining_us)
                if th.state == "RUNNING" and th.spec.policy in RR_POLICIES:
                    limits.append(th.rr_remaining_us)
                if self.m.aps_enabled:
                    limits.append(self._aps_remaining(th, th.cpu or 0))
            dt = min(limits) if limits else 0.0
            if dt < 0:
                dt = 0.0

            if dt > 0:
                self._advance(dt)

            # Re-evaluate after any step completions / preemptions.
            self._recompute_priorities()
            self._dispatch()
            if dt <= 0:
                # No runnable work: jump to the next event; do not spin.
                if self._heap:
                    t_next = self._heap[0][0]
                    if t_next > self.now:
                        for cpu, th in enumerate(self.cpu_running):
                            if th is None:
                                self._trace.idle(self.now, t_next, cpu)
                        self.now = t_next
                    self._process_events_at(self.now)
                    self._dispatch()
                else:
                    break

        # Finalise incomplete jobs.
        for th in self.threads.values():
            rec = th.active_job
            if rec is not None and rec.complete_us is None:
                rec.status = "INCOMPLETE"
        for rec in self._trace.jobs:
            if rec.complete_us is None:
                rec.status = "INCOMPLETE"
        self._trace.meta.update({
            "aps_free_time_us": round(self.free_time_us, 3),
            "aps_bankruptcies": self.bankruptcy_events,
            "iterations": self._iterations,
        })
        return self._trace.sort()

    def _advance(self, dt: float) -> None:
        start = self.now
        end = start + dt
        for cpu, th in enumerate(self.cpu_running):
            if th is None:
                self._trace.idle(start, end, cpu)
                continue
            self._trace.run(start, end, th.spec.name, cpu,
                            th.effective_priority, th.spec.partition)
            self._aps_account(th, dt)
            th.remaining_us -= dt
            if th.spec.policy in RR_POLICIES:
                th.rr_remaining_us -= dt
            if th.spec.policy == "SPORADIC" and th.sporadic_left_us > 0:
                th.sporadic_left_us = max(0.0, th.sporadic_left_us - dt)
                if th.sporadic_left_us == 0:
                    self._emit(end, "sporadic_drop", th.spec.name,
                               detail="budget exhausted, low priority until replenish")
                    self._push(end + th.spec.sporadic_period_us,
                               "sporadic_replenish", th)
        self.now = end

        # Step completions (after time has moved, so priority changes can
        # preempt at the right instant).
        for th in list(self.cpu_running):
            if th is None:
                continue
            if th.remaining_us <= 1e-9:
                th.remaining_us = 0.0
                if th.spec.role == "server":
                    self._finish_server_message(th)
                else:
                    th.pc += 1
                    self._execute(th)
            elif th.spec.policy in RR_POLICIES and th.rr_remaining_us <= 1e-9:
                th.state = "READY"
                th.cpu = None
                th.ready_seq = self._next_seq()
                self._state_event(th, "READY", detail="RR quantum expired")
        if self.collect_events and self.now > 0:
            # Periodic CONTROL:TIME marker, like the instrumented kernel.
            pass

    def _process_events_at(self, t: float) -> None:
        due: list[tuple[int, str, Any]] = []
        while self._heap and self._heap[0][0] <= t + 1e-9:
            _, seq, kind, payload = heapq.heappop(self._heap)
            due.append((seq, kind, payload))
        for seq, kind, payload in due:
            if kind == "end":
                continue
            if kind == "job_release":
                self._on_job_release(*payload)
            elif kind == "wake":
                th, why = payload
                th.pc += 1 if why == "sleep" else 0
                self._wake(th, f"{why} expired")
                self._execute(th)
            elif kind == "irq":
                self._on_irq(*payload)
            elif kind == "aps_window":
                self._on_aps_window()
            elif kind == "sporadic_replenish":
                th: ThreadRT = payload
                th.sporadic_left_us = float(th.spec.sporadic_budget_us)
                self._emit(t, "sporadic_replenish", th.spec.name,
                           detail="budget replenished")
                self._recompute_priorities()
            elif kind == "control_time":
                self._emit(t, "control_time", "", detail="kernel time marker")
            else:  # pragma: no cover
                raise SimulationError(f"unknown event kind: {kind}")


def simulate(model: SystemModel, collect_events: bool = True) -> Trace:
    """Convenience wrapper: build a :class:`Kernel` and run it."""
    return Kernel(model, collect_events=collect_events).run()
