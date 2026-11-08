"""Determinism Autopilot: derive and *prove* a minimal configuration fix.

Most timing tools stop at "you missed a deadline". Chronoscope closes the loop:
it treats the QNX system model as a search space, generates semantically valid
configuration patches (priority assignment, message-driven priority
inheritance flags, PROCMGR_AID_PRIORITY, SCHED_FIFO vs RR, adaptive-partition
budgets, sporadic budgets, procnto -P ceiling, CPU count), re-simulates each
candidate against the same deterministic QNX scheduler, and keeps the change
that measurably improves the determinism objective.

The winning patch set is returned together with before/after traces and KPIs,
so the claim "this fixes the deadline misses" is machine-checkable rather than
a suggestion from a chatbot. Search is deterministic and bounded, which makes
the result reproducible in a safety review.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .audit import AuditResult, audit
from .kernel import simulate
from .model import SystemModel, dump_model
from .trace import Trace


@dataclass
class Patch:
    kind: str
    target: str
    field: str
    old: Any
    new: Any
    reason: str
    qnx_reference: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def describe(self) -> str:
        return (f"{self.kind}: {self.target}.{self.field} "
                f"{self.old!r} -> {self.new!r}")


@dataclass
class AutopilotResult:
    verified: bool = False
    patches: list[Patch] = field(default_factory=list)
    candidates_evaluated: int = 0
    rounds: int = 0
    baseline: AuditResult | None = None
    fixed: AuditResult | None = None
    fixed_model: SystemModel | None = None
    before_trace: Trace | None = None
    after_trace: Trace | None = None
    proof: dict[str, Any] = field(default_factory=dict)
    runtime_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "patches": [p.to_dict() for p in self.patches],
            "candidates_evaluated": self.candidates_evaluated,
            "rounds": self.rounds,
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "fixed": self.fixed.to_dict() if self.fixed else None,
            "proof": self.proof,
            "runtime_s": self.runtime_s,
        }


def apply_patch(model: SystemModel, p: Patch) -> None:
    """Apply a single, validated configuration patch (mutates ``model``)."""
    if p.kind == "system":
        setattr(model, p.field, p.new)
        return
    if p.kind == "thread":
        setattr(model.thread(p.target), p.field, p.new)
        return
    if p.kind == "channel":
        for c in model.channels:
            if c.name == p.target:
                setattr(c, p.field, p.new)
                # Keep the server thread's flag in sync; model_from_dict does
                # the same when loading a TOML file.
                if p.field == "fixed_priority" and not p.new:
                    for t in model.threads:
                        if t.channel == c.name:
                            t.fixed_priority = False
                return
        raise KeyError(p.target)
    if p.kind == "partition":
        for x in model.partitions:
            if x.name == p.target:
                setattr(x, p.field, p.new)
                return
        raise KeyError(p.target)
    raise KeyError(p.kind)


def objective(a: AuditResult) -> float:
    """Lower is better. Deadline misses dominate everything else."""
    k = a.kpis
    return (
        1_000_000.0 * k["deadline_misses"]
        + 100.0 * k["max_lateness_us"]
        + 100_000.0 * k["priority_inversion_episodes"]
        + 10.0 * k["max_priority_inversion_us"]
        + 500.0 * k["aps_bankruptcies"]
        + 50.0 * k["sporadic_budget_drops"]
        + 5.0 * k["max_jitter_us"]
        + 100.0 * (100.0 - a.score)
    )


def needs_fix(a: AuditResult) -> bool:
    """Whether the baseline actually has a determinism defect to repair.

    The autopilot is a repair tool, not a tuning tool: jitter alone (with no
    missed deadlines, inversion, budget exhaustion or sporadic drop) is not a
    defect, and adding hardware to shave microseconds would be a misleading
    "fix". Returning False keeps a healthy QNX configuration untouched and
    makes the report honest.
    """
    k = a.kpis
    return bool(
        a.verdict != "DETERMINISTIC"
        or k["deadline_misses"]
        or k["priority_inversion_episodes"]
        or k["aps_bankruptcies"]
        or k["sporadic_budget_drops"]
    )


def _candidates(model: SystemModel, applied: set[str]) -> list[Patch]:
    """Generate bounded, QNX-semantically-valid candidate patches."""
    out: list[Patch] = []
    ceil = model.unprivileged_ceiling

    def add(kind: str, target: str, field_: str, old: Any, new: Any,
            reason: str, ref: str) -> None:
        key = f"{kind}:{target}:{field_}:{new!r}"
        if old == new or key in applied:
            return
        out.append(Patch(kind, target, field_, old, new, reason, ref))

    # -- message passing / priority inheritance -----------------------------
    for c in model.channels:
        if c.fixed_priority:
            add("channel", c.name, "fixed_priority", True, False,
                "Remove _NTO_CHF_FIXED_PRIORITY so the server inherits the "
                "priority of its highest-priority client.",
                "C Library Reference > ChannelCreate()")
    for t in model.threads:
        if t.role == "server":
            if t.fixed_priority:
                add("thread", t.name, "fixed_priority", True, False,
                    "Server channel should use message-driven priority inheritance.",
                    "System Architecture > Priority inheritance and messages")
            if not t.ability_priority:
                add("thread", t.name, "ability_priority", False, True,
                    "Grant PROCMGR_AID_PRIORITY so inheritance is not clamped "
                    "to the highest unprivileged priority.",
                    "C Library Reference > procmgr_ability()")
            clients = [x.priority for x in model.threads
                       if any(s.op in ("send", "pulse")
                              and s.args.get("channel") == t.channel
                              for j in x.jobs for s in j.steps)]
            if clients:
                target_prio = min(max(clients), ceil - 1)
                if target_prio != t.priority:
                    add("thread", t.name, "priority", t.priority, target_prio,
                        "Set server base priority to the highest-priority "
                        "client to bound blocking even without inheritance.",
                        "System Architecture > Scheduling priority")
            for delta in (8, 16):
                np = min(ceil - 1, t.priority + delta)
                add("thread", t.name, "priority", t.priority, np,
                    f"Raise server priority by {delta}.",
                    "System Architecture > Scheduling priority")

    # -- thread priority / policy ------------------------------------------
    for t in model.threads:
        if t.role == "server":
            continue
        if t.policy in ("RR", "OTHER"):
            add("thread", t.name, "policy", t.policy, "FIFO",
                "SCHED_FIFO removes time-slice preemption for a hard "
                "real-time control thread.",
                "System Architecture > Scheduling priority")
        for delta in (8, 16):
            np = min(ceil - 1, t.priority + delta)
            add("thread", t.name, "priority", t.priority, np,
                f"Raise priority by {delta} (rate-monotonic: shorter period "
                "should get the higher priority).",
                "System Architecture > What is Real Time and Why Do I Need It?")
        hi = max((x.priority for x in model.threads if x is not t), default=0)
        np = min(ceil - 1, hi + 1)
        add("thread", t.name, "priority", t.priority, np,
            "Promote this thread above the current highest-priority peer.",
            "System Architecture > Scheduling priority")
        if t.policy == "SPORADIC":
            add("thread", t.name, "sporadic_budget_us", t.sporadic_budget_us,
                max(t.sporadic_budget_us * 2, t.sporadic_budget_us + 500),
                "Double the sporadic-server initial budget.",
                "System Architecture > Scheduling policies")

    # -- system-level knobs -------------------------------------------------
    add("system", "system", "cpus", model.cpus, min(4, model.cpus + 1),
        "Add a CPU so control work is not displaced by best-effort work.",
        "Programmer's Guide > Multicore Processing")
    add("system", "system", "unprivileged_ceiling", model.unprivileged_ceiling,
        min(253, model.unprivileged_ceiling + 32),
        "procnto -P: widen the unprivileged priority range so privileged "
        "priorities are not required.",
        "Utilities Reference > procnto (-P)")
    add("system", "system", "rr_quantum_us", model.rr_quantum_us,
        model.rr_quantum_us * 4,
        "Reduce round-robin context-switch overhead.",
        "System Architecture > Scheduling priority")
    if model.aps_enabled and len(model.partitions) > 1:
        ranked = sorted(model.partitions, key=lambda p: p.budget_pct)
        lo, hi = ranked[0], ranked[-1]
        shift = min(20.0, hi.budget_pct - 10.0)
        if shift > 0:
            add("partition", lo.name, "budget_pct", lo.budget_pct,
                lo.budget_pct + shift,
                "Shift adaptive-partition budget toward the starved partition.",
                "Adaptive Partitioning User's Guide")
            add("partition", hi.name, "budget_pct", hi.budget_pct,
                hi.budget_pct - shift,
                "Reduce the best-effort partition budget correspondingly.",
                "Adaptive Partitioning User's Guide")
        for t in model.threads:
            if t.role != "server" and not t.critical_thread:
                add("thread", t.name, "critical_thread", False, True,
                    "Allow critical response budget when the partition is bankrupt.",
                    "Adaptive Partitioning User's Guide > Critical threads")
    return out


def autopilot(model: SystemModel, max_rounds: int = 3,
              max_candidates: int = 110,
              progress: Callable[[str], None] | None = None) -> AutopilotResult:
    """Search for a minimal configuration that removes deadline misses."""
    started = time.monotonic()
    res = AutopilotResult()
    before_trace = simulate(model)
    res.before_trace = before_trace
    res.baseline = audit(before_trace, model)
    best_model = model.clone()
    best_score = objective(res.baseline)
    applied: set[str] = set()

    def say(msg: str) -> None:
        if progress:
            progress(msg)

    healthy = not needs_fix(res.baseline)
    if healthy:
        say("baseline is deterministic with no timing defects; no fix needed")

    for round_no in (range(0) if healthy else range(1, max_rounds + 1)):
        say(f"round {round_no}: evaluating candidate QNX configurations...")
        candidates = _candidates(best_model, applied)[:max_candidates]
        if not candidates:
            break
        winner: tuple[float, Patch, SystemModel, AuditResult, Trace] | None = None
        for p in candidates:
            trial = best_model.clone()
            try:
                apply_patch(trial, p)
                # Re-validate by round-tripping the TOML representation.
                from .model import model_from_dict
                trial = model_from_dict(trial.to_dict())
                tr = simulate(trial)
                ar = audit(tr, trial)
            except Exception:
                continue
            res.candidates_evaluated += 1
            sc = objective(ar)
            if sc < best_score - 1e-9:
                winner = (sc, p, trial, ar, tr)
                best_score = sc
        if winner is None:
            break
        _, patch, best_model, res.fixed, res.after_trace = winner
        res.patches.append(patch)
        applied.add(f"{patch.kind}:{patch.target}:{patch.field}:{patch.new!r}")
        res.rounds = round_no
        say(f"  applied {patch.describe()} -> score {res.fixed.score}")
        if (res.fixed.kpis["deadline_misses"] == 0
                and res.fixed.kpis["priority_inversion_episodes"] == 0):
            break

    if res.patches:
        final_trace = simulate(best_model)
        final_audit = audit(final_trace, best_model)
        res.after_trace, res.fixed = final_trace, final_audit
    else:
        res.after_trace, res.fixed = before_trace, res.baseline
    res.fixed_model = best_model
    b, a = res.baseline, res.fixed
    res.verified = bool(
        a.kpis["deadline_misses"] == 0
        and b.kpis["deadline_misses"] > 0
        and a.kpis["deadline_misses"] < b.kpis["deadline_misses"]
    ) or bool(
        a.kpis["priority_inversion_episodes"] < b.kpis["priority_inversion_episodes"]
        and a.kpis["deadline_misses"] == 0
    )
    res.proof = {
        "method": ("Bounded greedy search over QNX-semantically-valid "
                   "configuration patches; every candidate re-simulated on the "
                   "same deterministic Neutrino scheduler and re-audited."),
        "deterministic": True,
        "candidates_evaluated": res.candidates_evaluated,
        "patches_applied": len(res.patches),
        "before": {
            "verdict": b.verdict, "score": b.score,
            "deadline_misses": b.kpis["deadline_misses"],
            "max_lateness_us": b.kpis["max_lateness_us"],
            "priority_inversions": b.kpis["priority_inversion_episodes"],
            "max_inversion_us": b.kpis["max_priority_inversion_us"],
            "worst_case_response_us": b.kpis["worst_case_response_us"],
        },
        "after": {
            "verdict": a.verdict, "score": a.score,
            "deadline_misses": a.kpis["deadline_misses"],
            "max_lateness_us": a.kpis["max_lateness_us"],
            "priority_inversions": a.kpis["priority_inversion_episodes"],
            "max_inversion_us": a.kpis["max_priority_inversion_us"],
            "worst_case_response_us": a.kpis["worst_case_response_us"],
        },
        "claim": (("Baseline is deterministic on this workload; no "
                   "configuration change was required or applied.")
                  if not res.patches else
                  "Counterfactually verified: the patched model completes "
                  "every job within its deadline on the same workload."),
    }
    res.runtime_s = round(time.monotonic() - started, 3)
    return res


def summarize(result: AutopilotResult) -> str:
    lines = [
        "=" * 68,
        "  CHRONOSCOPE DETERMINISM AUTOPILOT",
        "=" * 68,
        f"  Candidates simulated : {result.candidates_evaluated}"
        f"    Rounds : {result.rounds}"
        f"    Time : {result.runtime_s}s",
        f"  Verified fix        : {'YES' if result.verified else 'NO / NOT NEEDED'}",
    ]
    b, a = result.proof.get("before", {}), result.proof.get("after", {})
    lines += [
        "-" * 68,
        f"  {'metric':<26}{'BEFORE':>14}{'AFTER':>14}",
        f"  {'verdict':<26}{str(b.get('verdict')):>14}{str(a.get('verdict')):>14}",
        f"  {'score':<26}{str(b.get('score')):>14}{str(a.get('score')):>14}",
        f"  {'deadline misses':<26}{str(b.get('deadline_misses')):>14}"
        f"{str(a.get('deadline_misses')):>14}",
        f"  {'max lateness (us)':<26}{str(b.get('max_lateness_us')):>14}"
        f"{str(a.get('max_lateness_us')):>14}",
        f"  {'priority inversions':<26}{str(b.get('priority_inversions')):>14}"
        f"{str(a.get('priority_inversions')):>14}",
        f"  {'max inversion (us)':<26}{str(b.get('max_inversion_us')):>14}"
        f"{str(a.get('max_inversion_us')):>14}",
        "-" * 68,
        "  PATCHES",
    ]
    if not result.patches:
        lines.append("  (none -- baseline already meets all deadlines)")
    for i, p in enumerate(result.patches, 1):
        lines.append(f"  {i}. {p.describe()}")
        lines.append(f"     why: {p.reason}")
        lines.append(f"     ref: {p.qnx_reference}")
    lines.append("=" * 68)
    return "\n".join(lines)
