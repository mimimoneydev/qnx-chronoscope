"""Determinism audit: turn a QNX execution trace into a verdict and a fix list.

The audit answers the four questions a real-time engineer actually asks:

1. Did every job meet its deadline, and what is the worst-case response time?
2. Where exactly did priority inversion occur, for how long, and who caused it?
3. Are there unbounded blocking / interrupt-latency / partition-budget hazards?
4. Is there enough structured evidence to put into a timing safety case?

Every finding carries ``evidence`` (the concrete artifact a reviewer can open)
and ``qnx_reference`` (the QNX SDP 8.0 documentation section it derives from),
which is exactly what the hackathon's "Evidence of QNX use" clause asks for.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .anomaly import detect_anomalies
from .model import SystemModel
from .trace import JobRecord, Segment, Trace

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}


@dataclass
class Finding:
    severity: str
    code: str
    title: str
    detail: str
    evidence: str
    recommendation: str
    qnx_reference: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditResult:
    verdict: str = "DETERMINISTIC"
    score: float = 100.0
    kpis: dict[str, Any] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    per_thread: list[dict[str, Any]] = field(default_factory=list)
    inversions: list[dict[str, Any]] = field(default_factory=list)
    partition_usage: list[dict[str, Any]] = field(default_factory=list)
    timing_evidence: dict[str, Any] = field(default_factory=dict)
    anomalies: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "score": self.score,
            "kpis": self.kpis,
            "findings": [f.to_dict() for f in self.findings],
            "per_thread": self.per_thread,
            "inversions": self.inversions,
            "partition_usage": self.partition_usage,
            "timing_evidence": self.timing_evidence,
            "anomalies": self.anomalies,
        }


def _overlap(a: Segment, b: Segment) -> float:
    return max(0.0, min(a.end_us, b.end_us) - max(a.start_us, b.start_us))


def _priority_map(trace: Trace, model: SystemModel | None) -> dict[str, int]:
    prios: dict[str, int] = {}
    if model:
        for t in model.threads:
            prios[t.name] = t.priority
    # Observed run segments are the most reliable source (a real QNX trace
    # records the *effective* priority, including inheritance boosts).
    for s in trace.segments:
        if s.kind == "run" and s.priority:
            prios.setdefault(s.thread, s.priority)
    for j in trace.jobs:
        if j.priority:
            prios.setdefault(j.thread, j.priority)
    return prios


# Priority inversion is NOT "a high-priority thread is not running". It is
# "a high-priority thread is blocked on a resource that a lower-priority
# thread can hold, while that lower-priority thread runs". idle/voluntary
# waits -- MsgReceive() waiting for work, nanosleep(), InterruptWait() --
# are normal QNX behaviour and must never be reported as inversion.
#
# Simulated traces carry descriptive reasons ("STATE_SEND on actuator",
# "pthread_mutex_lock(brake_bus)", "waiting for worker work"); real
# traceprinter dumps carry the TH* state name ("SEND", "MUTEX", "RECEIVE").
# Both spellings are recognised here.
_CONTENTION_BLOCK_PREFIXES = (
    "SEND", "STATE_SEND", "MSGSEND", "REPLY", "STATE_REPLY",
    "MUTEX", "PTHREAD_MUTEX_LOCK", "SEM", "CONDVAR",
    "WAITPAGE", "NET_SEND", "NET_REPLY",
)
_IDLE_BLOCK_PREFIXES = (
    "RECEIVE", "MSGRECEIVE", "WAITING FOR", "NANOSLEEP", "INTR",
    "INTERRUPTWAIT", "SIGWAITINFO", "SIGSUSPEND", "STOPPED", "DEAD",
    "JOIN", "IDLE",
)


def _is_contended_block(reason: str) -> bool:
    """True only for blocking states another thread can be responsible for."""
    r = (reason or "").strip().upper()
    if not r or r.startswith(_IDLE_BLOCK_PREFIXES):
        return False
    return r.startswith(_CONTENTION_BLOCK_PREFIXES)


_CHANNEL_FROM_REASON = re.compile(r"\bon\s+([A-Za-z0-9_\-]+)")


def _server_of(reason: str, trace: Trace, t_us: float) -> str | None:
    """Find the QNX server thread that received this client's message.

    Works for both simulated and real traces because it uses the standard
    ``MsgReceive(<channel>)`` kernel-call event rather than a model file.
    """
    m = _CHANNEL_FROM_REASON.search(reason or "")
    if not m:
        return None
    want = f"MsgReceive({m.group(1)})"
    server = None
    for e in trace.events:
        if e.t_us > t_us + 1e-6:
            break
        if e.kind == "kernel_call" and e.detail == want:
            server = e.thread
    return server


def _find_inversions(trace: Trace, prios: dict[str, int]) -> list[dict[str, Any]]:
    """A high-priority thread is blocked on a contended resource while a
    lower-priority thread RUNS (the classic QNX unbounded inversion).

    Two guards keep this from reporting false positives:

    * if the receiving server ran at the client's priority (message-driven
      priority inheritance worked), the wait is expected, not an inversion;
    * on SMP targets only runs on the CPU the blocked thread last used can
      delay it -- an unrelated thread on another CPU cannot be the cause.
    """
    runs = [s for s in trace.segments if s.kind == "run"]
    blocks = [s for s in trace.segments
              if s.kind == "blocked" and _is_contended_block(s.reason)]
    episodes: list[dict[str, Any]] = []
    for b in blocks:
        p_blocked = b.priority or prios.get(b.thread, 0)
        if p_blocked <= 1:
            continue
        server = _server_of(b.reason, trace, b.start_us)
        if server and any(
                r.thread == server and r.priority >= p_blocked
                and r.start_us < b.end_us and r.end_us > b.start_us
                for r in runs):
            continue  # inheritance did its job
        culprits: dict[str, float] = {}
        for r in runs:
            if r.start_us >= b.end_us or r.end_us <= b.start_us:
                continue
            if trace.cpus > 1 and r.cpu != b.cpu:
                continue
            p_run = r.priority or prios.get(r.thread, 0)
            if p_run < p_blocked:
                culprits[r.thread] = culprits.get(r.thread, 0.0) + _overlap(b, r)
        for culprit, us in culprits.items():
            if us <= 0:
                continue
            episodes.append({
                "blocked_thread": b.thread,
                "blocked_priority": p_blocked,
                "blocked_reason": b.reason,
                "culprit": culprit,
                "culprit_priority": prios.get(culprit, 0),
                "start_us": round(b.start_us, 3),
                "end_us": round(b.end_us, 3),
                "duration_us": round(us, 3),
            })
    episodes.sort(key=lambda e: (-e["duration_us"], e["start_us"]))
    return episodes


def _per_thread(trace: Trace, prios: dict[str, int]) -> list[dict[str, Any]]:
    responses: dict[str, list[float]] = {}
    misses: dict[str, int] = {}
    cpu: dict[str, float] = {}
    for j in trace.jobs:
        r = j.response_us
        if r is not None:
            responses.setdefault(j.thread, []).append(r)
        if j.missed or j.status == "INCOMPLETE":
            misses[j.thread] = misses.get(j.thread, 0) + 1
    for s in trace.segments:
        if s.kind == "run":
            cpu[s.thread] = cpu.get(s.thread, 0.0) + s.duration_us
    rows: list[dict[str, Any]] = []
    for name in sorted(set(responses) | set(cpu) | set(prios)):
        xs = responses.get(name, [])
        rows.append({
            "thread": name,
            "priority": prios.get(name, 0),
            "jobs": len(xs),
            "misses": misses.get(name, 0),
            "min_us": round(min(xs), 1) if xs else None,
            "mean_us": round(sum(xs) / len(xs), 1) if xs else None,
            "wcet_us": round(max(xs), 1) if xs else None,
            "jitter_us": round(max(xs) - min(xs), 1) if len(xs) > 1 else 0.0,
            "cpu_us": round(cpu.get(name, 0.0), 1),
        })
    return rows


def audit(trace: Trace, model: SystemModel | None = None) -> AuditResult:
    """Produce a determinism verdict, findings and timing evidence."""
    prios = _priority_map(trace, model)
    jobs = trace.jobs
    completed = [j for j in jobs if j.response_us is not None]
    missed = [j for j in jobs if j.missed or j.status == "INCOMPLETE"]
    worst = max((j.response_us or 0.0 for j in completed), default=0.0)
    lateness = max((-(j.slack_us or 0.0) for j in missed), default=0.0)
    jitter = max((max(xs) - min(xs)
                  for xs in [[j.response_us for j in completed
                             if j.thread == t and j.response_us is not None]
                             for t in {j.thread for j in completed}] if len(xs) > 1),
                 default=0.0)
    runs = [s for s in trace.segments if s.kind == "run"]
    isrs = [s for s in trace.segments if s.kind == "isr"]
    busy = sum(s.duration_us for s in runs)
    horizon = max(trace.horizon_us, 1.0)
    util = 100.0 * busy / (horizon * max(trace.cpus, 1))
    inversions = _find_inversions(trace, prios)
    max_inv = max((e["duration_us"] for e in inversions), default=0.0)
    boosts = sum(1 for e in trace.events if e.kind == "priority_boost")
    sends = sum(1 for e in trace.events if e.kind == "kernel_call"
                and e.detail.startswith(("MsgSend(", "MsgSendPulse(")))
    replies = sum(1 for e in trace.events if e.kind == "kernel_call"
                  and e.detail.startswith("MsgReply("))
    rr_expiries = sum(1 for e in trace.events if e.kind == "READY"
                      and "RR quantum" in e.detail)
    sporadic_drops = sum(1 for e in trace.events if e.kind == "sporadic_drop")
    bankruptcies = trace.meta.get("aps_bankruptcies", [])

    # ISR -> IST latency from interrupt_entry to the IST job release.
    irq_entries = [e.t_us for e in trace.events if e.kind == "interrupt_entry"]
    ist_releases = [e.t_us for e in trace.events
                    if e.kind == "job_release" and e.thread.endswith("_ist")]
    isr_to_ist = 0.0
    if irq_entries and ist_releases:
        for t in irq_entries:
            nxt = [x for x in ist_releases if x >= t]
            if nxt:
                isr_to_ist = max(isr_to_ist, min(nxt) - t)

    anomalies = detect_anomalies(trace)
    r = AuditResult()
    r.kpis = {
        "total_jobs": len(jobs),
        "completed_jobs": len(completed),
        "deadline_misses": len(missed),
        "miss_rate_pct": round(100.0 * len(missed) / max(len(jobs), 1), 2),
        "worst_case_response_us": round(worst, 1),
        "max_lateness_us": round(lateness, 1),
        "max_jitter_us": round(jitter, 1),
        "cpu_utilization_pct": round(util, 2),
        "priority_inversion_episodes": len(inversions),
        "max_priority_inversion_us": round(max_inv, 1),
        "message_driven_priority_boosts": boosts,
        "msg_send_transactions": sends,
        "msg_reply_transactions": replies,
        "rr_quantum_expiries": rr_expiries,
        "sporadic_budget_drops": sporadic_drops,
        "aps_bankruptcies": len(bankruptcies),
        "isr_count": len(isrs),
        "max_isr_to_ist_us": round(isr_to_ist, 1),
        "horizon_us": round(horizon, 1),
        "cpus": trace.cpus,
        "trace_source": trace.source,
    }
    r.per_thread = _per_thread(trace, prios)
    r.inversions = inversions[:50]
    r.anomalies = [asdict(a) for a in anomalies]
    r.partition_usage = _partition_usage(trace, model)

    # -- findings ----------------------------------------------------------
    f: list[Finding] = []
    if missed:
        by_thread: dict[str, int] = {}
        for j in missed:
            by_thread[j.thread] = by_thread.get(j.thread, 0) + 1
        f.append(Finding(
            "CRITICAL", "DEADLINE_MISS",
            f"{len(missed)} of {len(jobs)} jobs miss their deadline",
            f"Worst lateness {lateness:.0f} us; affected threads: "
            + ", ".join(f"{k} ({v})" for k, v in sorted(by_thread.items()))
            + f". Worst-case response time {worst:.0f} us.",
            "trace.jobs[].response_us / status; chronoscope/audit.py:audit()",
            "Run `chronoscope fix` to derive a counterfactually verified "
            "priority/inheritance/partition configuration, then re-audit.",
            "System Architecture > What is Real Time and Why Do I Need It?"))
    if inversions:
        worst_e = inversions[0]
        f.append(Finding(
            "HIGH", "PRIORITY_INVERSION",
            f"{len(inversions)} priority-inversion episode(s), longest {max_inv:.0f} us",
            f"{worst_e['blocked_thread']} (prio {worst_e['blocked_priority']}) "
            f"was {worst_e['blocked_reason']} while {worst_e['culprit']} "
            f"(prio {worst_e['culprit_priority']}) ran for "
            f"{worst_e['duration_us']:.0f} us.",
            "trace.segments (kind='blocked', contended STATE_SEND/REPLY/"
            "MUTEX/SEM states only) overlapped with kind='run'; "
            "chronoscope/audit.py:_find_inversions()",
            "Enable QNX message-driven priority inheritance (remove "
            "_NTO_CHF_FIXED_PRIORITY), grant PROCMGR_AID_PRIORITY to the "
            "server, or raise the server's base priority.",
            "System Architecture > Priority inheritance and messages"))
    if model:
        for c in model.channels:
            if c.fixed_priority:
                hi = [t for t in model.threads
                      if any(st.op == "send" and st.args.get("channel") == c.name
                             for j in t.jobs for st in j.steps)
                      and t.priority > model.thread(c.server).priority]
                if hi:
                    f.append(Finding(
                        "HIGH", "FIXED_PRIORITY_CHANNEL",
                        f"Channel {c.name!r} disables priority inheritance",
                        f"_NTO_CHF_FIXED_PRIORITY is set while "
                        f"{len(hi)} higher-priority client(s) send to a "
                        f"lower-priority server ({c.server}). This is the "
                        "classic unbounded priority inversion.",
                        f"models/*.toml [[channels]] fixed_priority; "
                        f"target/resmgr/chronoscope_resmgr.c ChannelCreate()",
                        "Clear _NTO_CHF_FIXED_PRIORITY, or use "
                        "pthread_mutexattr_setprotocol(PTHREAD_PRIO_INHERIT).",
                        "C Library Reference > ChannelCreate()"))
        for t in model.threads:
            if t.role == "server" and not t.ability_priority:
                hi = [x for x in model.threads if x.priority >= model.unprivileged_ceiling
                      and any(st.op in ("send", "pulse")
                              and st.args.get("channel") == t.channel
                              for j in x.jobs for st in j.steps)]
                if hi:
                    f.append(Finding(
                        "MEDIUM", "MISSING_PRIORITY_ABILITY",
                        f"Server {t.name!r} cannot inherit privileged priorities",
                        "Without PROCMGR_AID_PRIORITY the kernel clamps a "
                        "boosted server to the highest unprivileged priority "
                        f"({model.unprivileged_ceiling - 1}), so a privileged "
                        "client still waits behind medium-priority work.",
                        "models/*.toml ability_priority=false; "
                        "target/resmgr/chronoscope_resmgr.c procmgr_ability()",
                        "Grant PROCMGR_AID_PRIORITY with procmgr_ability() "
                        "(or an appropriate secpol policy) to the resource manager.",
                        "C Library Reference > procmgr_ability(); "
                        "System Security Guide"))
        for t in model.threads:
            if t.priority >= model.unprivileged_ceiling and not t.ability_priority:
                f.append(Finding(
                    "MEDIUM", "PRIVILEGED_PRIORITY",
                    f"Thread {t.name!r} uses priority {t.priority} without an ability",
                    f"Levels {model.unprivileged_ceiling}..253 require "
                    "PROCMGR_AID_PRIORITY on the QNX 8.0 target. On a default "
                    "image this thread fails to start or is clamped.",
                    f"models/*.toml priority={t.priority}; "
                    "target/images/chronoscope.build (procnto -P)",
                    "Use procnto-smp-instr -P <ceiling> at boot or grant the "
                    "ability; see the codelab step 4.",
                    "System Architecture > Scheduling priority; Utilities > procnto"))
    if bankruptcies:
        f.append(Finding(
            "MEDIUM", "APS_BANKRUPTCY",
            f"{len(bankruptcies)} adaptive-partition budget exhaustion(s)",
            "A partition consumed its percentage budget inside the averaging "
            "window; its threads were throttled until the window rolled over.",
            "trace.meta.aps_bankruptcies; chronoscope/kernel.py:_on_aps_window()",
            "Rebalance partition budgets, mark genuinely critical threads "
            "critical, or shorten the workload in the non-critical partition.",
            "Adaptive Partitioning User's Guide > Bankruptcy"))
    if sporadic_drops:
        f.append(Finding(
            "MEDIUM", "SPORADIC_BUDGET",
            f"SCHED_SPORADIC budget exhausted {sporadic_drops} time(s)",
            "The thread dropped to its low priority until replenishment, "
            "which can turn a bounded response into a missed deadline.",
            "trace.events[kind='sporadic_drop']; chronoscope/kernel.py:_advance()",
            "Increase initial budget or reduce replenishment period.",
            "System Architecture > Scheduling policies"))
    if rr_expiries > 10:
        f.append(Finding(
            "LOW", "RR_QUANTUM_THRASH",
            f"{rr_expiries} round-robin quantum expiries",
            "Frequent quantum expiry at a busy priority adds context-switch "
            "overhead that a hard-real-time design usually avoids.",
            "trace.events[kind='READY'][detail='RR quantum expired']",
            "Use SCHED_FIFO for the control-loop threads, or raise the quantum.",
            "System Architecture > Scheduling priority"))
    if isr_to_ist > 0:
        f.append(Finding(
            "LOW", "ISR_TO_IST_LATENCY",
            f"Maximum ISR-to-IST latency {isr_to_ist:.0f} us",
            "Interrupt stubs are non-preemptible on this model; the IST is "
            "released after the stub and any higher-priority in-kernel ISTs.",
            "trace.events[kind='interrupt_entry'|'job_release']",
            "Keep the ISR stub minimal; do the work in the IST (QNX "
            "InterruptAttachEvent / SIGEV_INTR).",
            "Programmer's Guide > Handling Hardware Interrupts"))
    if util > 85:
        f.append(Finding(
            "MEDIUM", "HIGH_UTILIZATION",
            f"CPU utilisation {util:.1f}% leaves little timing margin",
            "A rate-monotonic system with high utilisation has almost no "
            "headroom for WCET variance or interrupt storms.",
            "trace.segments[kind='run'] / horizon_us",
            "Reduce WCET assumptions, move best-effort work to another CPU, "
            "or put it in a separate adaptive partition.",
            "System Architecture > What is Real Time and Why Do I Need It?"))
    for a in anomalies[:3]:
        f.append(Finding(
            "MEDIUM", "TIMING_ANOMALY",
            f"{a.kind} on {a.thread} (job #{a.index}, {a.value_us:.1f} us)",
            a.explanation,
            "chronoscope/anomaly.py:detect_anomalies() robust z-score / CUSUM",
            "Compare with a traceprinter dump from the QNX 8.0 target using "
            "`chronoscope reconcile`.",
            "System Analysis Toolkit User's Guide > Events and the Kernel"))
    if boosts and not inversions:
        f.append(Finding(
            "INFO", "MESSAGE_PI_ACTIVE",
            f"QNX message-driven priority inheritance fired {boosts} time(s)",
            "Servers were boosted to the priority of their highest-priority "
            "client at send time, preventing inversion.",
            "trace.events[kind='priority_boost']; "
            "chronoscope/kernel.py:_boost()",
            "No action. Preserve this behaviour in the QNX build (do not set "
            "_NTO_CHF_FIXED_PRIORITY for safety-critical channels).",
            "System Architecture > Priority inheritance and messages"))
    if not missed and not inversions:
        f.append(Finding(
            "INFO", "DEADLINES_MET",
            "All modelled jobs meet their deadlines",
            f"{len(completed)} jobs completed; worst-case response "
            f"{worst:.0f} us, max jitter {jitter:.0f} us.",
            "trace.jobs[]; chronoscope/audit.py:audit()",
            "Validate against a real QNX 8.0 target with "
            "`chronoscope reconcile` before freezing WCET assumptions.",
            "System Analysis Toolkit User's Guide"))

    f.sort(key=lambda x: SEVERITY_ORDER.get(x.severity, 9))
    r.findings = f

    # -- verdict / score ----------------------------------------------------
    penalty = 0.0
    penalty += min(45.0, 9.0 * len(missed))
    penalty += min(25.0, 3.0 * len(inversions) + max_inv / 200.0)
    penalty += min(10.0, 2.0 * len(bankruptcies))
    penalty += min(8.0, 1.5 * sporadic_drops)
    penalty += 3.0 if len(anomalies) > 3 else 0.0
    penalty += 3.0 if util > 90 else 0.0
    r.score = round(max(0.0, 100.0 - penalty), 1)
    if missed or any(x.severity == "CRITICAL" for x in f):
        r.verdict = "NON-DETERMINISTIC"
    elif inversions or bankruptcies or r.score < 85:
        r.verdict = "AT RISK"
    else:
        r.verdict = "DETERMINISTIC"

    r.timing_evidence = {
        "purpose": ("Structured timing evidence for an ISO 26262 / IEC 62304 "
                    "/ DO-178C style safety case. This is engineering evidence, "
                    "NOT a certification artifact or a claim of compliance."),
        "tool": "Chronoscope for QNX 8.0 (host-based determinism workbench)",
        "qnx_version": "8.0",
        "trace_source": trace.source,
        "trace_tool": trace.meta.get("source_tool", "Chronoscope simulator"),
        "scheduling_model": ("QNX Neutrino: 256 priority levels, "
                             "message-driven priority inheritance, "
                             "SCHED_FIFO/RR/SPORADIC, adaptive partitioning"),
        "wcet_and_jitter_per_thread": r.per_thread,
        "deadline_miss_count": len(missed),
        "worst_case_response_us": round(worst, 1),
        "max_priority_inversion_us": round(max_inv, 1),
        "max_isr_to_ist_us": round(isr_to_ist, 1),
        "cpu_utilization_pct": round(util, 2),
        "priority_assignment": {k: v for k, v in sorted(prios.items())},
        "partition_budget_consumption": r.partition_usage,
        "reproducibility": ("Deterministic discrete-event replay; re-run "
                            "`chronoscope simulate MODEL` to reproduce exactly."),
    }
    return r


def _partition_usage(trace: Trace, model: SystemModel | None) -> list[dict[str, Any]]:
    if not model or not model.aps_enabled:
        return []
    used: dict[str, float] = {}
    for s in trace.segments:
        if s.kind == "run" and s.partition:
            used[s.partition] = used.get(s.partition, 0.0) + s.duration_us
    total = sum(used.values()) or 1.0
    return [{
        "partition": p.name,
        "budget_pct": p.budget_pct,
        "measured_pct": round(100.0 * used.get(p.name, 0.0) / total, 2),
        "used_us": round(used.get(p.name, 0.0), 1),
        "bankruptcies": sum(1 for b in trace.meta.get("aps_bankruptcies", [])
                            if b.get("partition") == p.name),
    } for p in model.partitions]


def summarize(result: AuditResult, name: str = "") -> str:
    """Render a compact terminal-friendly audit summary."""
    k = result.kpis
    bar = "=" * 68
    lines = [
        bar,
        f"  CHRONOSCOPE DETERMINISM AUDIT  {name}".rstrip(),
        bar,
        f"  Verdict : {result.verdict}    Score : {result.score}/100",
        f"  Jobs    : {k['completed_jobs']}/{k['total_jobs']} completed"
        f"    Deadline misses : {k['deadline_misses']} ({k['miss_rate_pct']}%)",
        f"  WCET    : {k['worst_case_response_us']} us"
        f"    Max jitter : {k['max_jitter_us']} us"
        f"    Max lateness : {k['max_lateness_us']} us",
        f"  Inversions : {k['priority_inversion_episodes']}"
        f"    longest {k['max_priority_inversion_us']} us"
        f"    CPU : {k['cpu_utilization_pct']}%",
        f"  QNX IPC : {k['msg_send_transactions']} MsgSend, "
        f"{k['msg_reply_transactions']} MsgReply, "
        f"{k['message_driven_priority_boosts']} PI boosts, "
        f"{k['isr_count']} ISRs",
        "-" * 68,
        "  FINDINGS",
    ]
    if not result.findings:
        lines.append("  (none)")
    for f in result.findings:
        lines.append(f"  [{f.severity:<8}] {f.code}: {f.title}")
        lines.append(f"             {f.detail}")
        lines.append(f"             fix: {f.recommendation}")
    lines.append(bar)
    return "\n".join(lines)
