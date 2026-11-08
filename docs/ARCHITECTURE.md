# Architecture & design decisions

Chronoscope is a **host-based determinism workbench for QNX Neutrino 8.0**.
This document explains why it is built the way it is, and where the boundaries
of the model lie.

---

## 1. Guiding decisions

| Decision | Rationale |
|---|---|
| **Python 3.11+ standard library only** | A QNX toolchain is licensed and platform-specific. Judges, CI and safety engineers should be able to `git clone` and run it in 60 seconds on macOS/Linux/Windows with no `pip install`, no network and no QNX licence. `tomllib` (3.11+) handles the model format. |
| **`Trace` is the single shared currency** | Simulation, real-trace import, audit, autopilot, reconciliation, anomaly detection and reporting all consume the same dataclass. This makes "simulated vs measured" a first-class comparison rather than two incompatible formats. |
| **Discrete-event simulation, not emulation** | The goal is to explore *configuration* (priorities, policies, channel flags, partitions, CPU count) deterministically and cheaply — 18 counterfactual runs in a few tens of milliseconds on a laptop. A cycle-accurate `procnto` emulator would be slower, less portable and would still not replace target measurement. |
| **No LLM in the analysis path** | A safety argument needs a reproducible answer. Median/MAD z-scores, CUSUM and EWMA are O(n), deterministic and auditable. The "AI creativity" is in the counterfactual configuration search, not in a chatbot suggesting priorities. |
| **Self-contained offline HTML** | Reports are opened in reviews, air-gapped networks and build containers. No CDN, fonts, JS libraries or external URLs. |
| **QNX target code ships even though it can't be compiled here** | The rules require the repository to show the project genuinely builds for QNX 8.0. The `qcc` Makefile, `mkifs` buildfile and QNX-only C APIs are that evidence; `target/README.md` is explicit about what was and wasn't executed. |
| **Honest scope statements everywhere** | Reports and docs say "engineering evidence, not a certification artifact". A fabricated on-target trace would be a much worse outcome than an acknowledged limitation. |

---

## 2. Module map

```
chronoscope/
├── model.py        TOML model, QNX constants, validation, canonical TOML writer
├── kernel.py       discrete-event Neutrino 8.0 scheduler
├── trace.py        Trace / TraceEvent / Segment / JobRecord, traceprinter export
├── importers.py    traceprinter / slog2info / pidin parsers, reconcile()
├── audit.py        verdict, KPIs, findings, structured timing evidence
├── anomaly.py      robust z-score, CUSUM changepoints, EWMA control chart
├── autopilot.py    bounded counterfactual configuration search
├── buildfile.py    mkifs buildfile parser + 12 determinism checks
├── report.py       standalone HTML / Markdown / JSON rendering
├── cli.py          argparse CLI (11 commands)
└── __main__.py     python3 -m chronoscope

models/             six declarative QNX 8.0 systems
target/             QNX SDP 8.0 C probe, resource manager, demo, mkifs image
codelab/            single-source codelab content + generator
docs/               GitHub Pages codelab (index.html), submission pack, this file
demo/               generated reports, proof, sample kernel trace, demo video
examples/traces/    sample QNX tool output for trying the importers
tests/              69 unittest cases
```

### Data flow

```
TOML model ─► Kernel.run() ─► Trace ─┬─► audit() ─► AuditResult ─┐
                                    │                            ├─► report.py ─► HTML/MD/JSON
QNX artifacts ─► importers.py ─► Trace ─┤                            │
                                    │  └─► autopilot() ─► patched model ──┘
                                    └─► reconcile() ─► drift report
                                    └─► anomaly.py ─► findings
mkifs .build ─► buildfile.py ─► BuildfileReport (independent of Trace)
```

`Trace.sort()` is time-stable and applied at every boundary so downstream code
never has to reason about ordering.

---

## 3. QNX 8.0 semantics implemented

| Area | Implementation | QNX 8.0 reference |
|---|---|---|
| 256 priority levels | `0` idle; `1..unprivileged_ceiling-1` unprivileged; `ceiling..253` privileged; `254` clock/in-kernel IST; `255` per-CPU IPI IST. Default ceiling 63 (`procnto -P`). | System Architecture > Scheduling priority |
| Scheduling policies | FIFO, RR (quantum), OTHER, SPORADIC (budget, replenishment, low-priority degradation) | System Architecture > Scheduling policies |
| Native message passing | `STATE_SEND` → `STATE_REPLY` → `MsgReply()`; synchronous client blocking; server `RECEIVE` state | System Architecture > IPC |
| Message-driven priority inheritance | Boost at **send** time when the sender outranks the server; no revert on reply; lowering on the next lower-priority receive | System Architecture > Priority inheritance and messages |
| `_NTO_CHF_FIXED_PRIORITY` | Per-channel flag that disables inheritance; also mirrored onto the server thread so TOML and C agree | C Library Reference > ChannelCreate() |
| `PROCMGR_AID_PRIORITY` | Inheritance clamped to `unprivileged_ceiling - 1` unless the server holds the ability; cap `253` otherwise | C Library Reference > procmgr_ability() |
| Pulses | `MsgSendPulse()` with priority carrying; `_NTO_SIDE_CHANNEL` in the C demo | C Library Reference > MsgSendPulse() |
| Mutexes | Priority inheritance / priority-protect protocols; waiter queues and priority recomputation | C Library Reference > pthread_mutexattr_setprotocol() |
| Interrupts | ISR stub segments, IST thread release, ISR→IST latency KPI | System Architecture > Interrupts |
| Adaptive partitioning | Percentage budgets, free-time borrowing by ratio, critical response budget, bankruptcy events | Adaptive Partitioning User's Guide |
| SMP | Per-CPU dispatch queues, affinity/runmasks, CPU-tagged blocked segments | Programmer's Guide > Multicore Processing |

**SMP-aware inversion detection** is deliberate: a lower-priority thread
running on a *different* CPU cannot be the cause of a blocked thread's wait.
`Trace.block()` records the CPU the thread last ran on; `audit._find_inversions()`
skips cross-CPU candidates. It also does not flag idle/voluntary waits
(`NANOSLEEP`, `RECEIVE`, `INTR`) and does not call expected QNX
message-driven inheritance an inversion: if the receiving server ran at or
above the blocked client's priority, the wait is *working as designed*.

---

## 4. Trace format

Internal `Trace` has four collections:

* `events` — point-in-time `TraceEvent(t_us, kind, thread, cpu, priority, pid,
  tid, detail, qnx_class, qnx_event)`.
* `segments` — intervals `Segment(start_us, end_us, thread, job, kind, cpu,
  priority, reason)` with kinds `run`, `blocked`, `idle`, `isr`.
* `jobs` — `JobRecord(thread, job, release_us, deadline_us, complete_us,
  status, cpu_us, blocked_us, priority, source, index)`. `deadline_us` is
  **absolute**; `missed` is `response_us > (deadline_us - release_us)`.
* `meta` — source tool, QNX version, counters, thread priority map.

`to_traceprinter_text()` emits a QNX-compatible text stream:

```
TRACE_SYS_RELEASE:: 8.0
t:0x00000000 CPU:00 PROCESS :PROCCREATE name:pedal_sampler pid:11 tid:0
t:0x0000abcd CPU:00 THREAD  :THRUNNING  pid:11 tid:1 prio:50
t:0x0000abcd CPU:00 KER_CALL :MsgSend(brake) ...
t:0x0000abcd CPU:00 CHRONO   :JOB_RELEASE pedal_sampler pedal_cycle#0 deadline=1200
```

Chronoscope-only annotations live under the separate `CHRONO` class so they
cannot be mistaken for real kernel events. `PROCESS:PROCCREATE` lines preserve
process names and `prio:<n>` tags preserve effective priorities through the
round trip. `parse_traceprinter()` reconstructs run/blocked segments, states,
kernel calls, priorities and jobs from this stream.

**C probe wire format** (`chrono_emit()` in `target/lib/chronoscope.c`):

```
CHRONO <monotonic_us> <thread> <KIND> <detail>
```

`<monotonic_us>` is an unsigned integer count of `CLOCK_MONOTONIC`
microseconds. `JOB_RELEASE` carries `deadline=<absolute_us>` and
`JOB_COMPLETE` carries `status=... response=<us>`. Both the bare-line
(stderr fallback) and full `slog2info` line forms reconstruct `JobRecord`s via
the shared `_chrono_job()` helper. `tests/test_target_contract.py` asserts the
C format string and the import behaviour so the two sides cannot drift.

---

## 5. Audit

`audit(trace, model)` produces an `AuditResult` with:

* **Verdict / score** — `DETERMINISTIC` vs `NON-DETERMINISTIC`, 0–100 score.
* **KPIs** — total/completed jobs, deadline misses and miss rate, WCET, max
  lateness, max jitter, CPU utilisation, inversion episodes and longest
  inversion, APS bankruptcies, sporadic budget drops, ISR→IST latency,
  `MsgSend`/`MsgReply` transaction counts, message-driven PI boost count.
* **Findings** — severity, code, message, evidence, recommendation, QNX 8.0
  reference. Examples: `DEADLINE_MISS`, `PRIORITY_INVERSION`,
  `FIXED_PRIORITY_CHANNEL`, `APS_BANKRUPTCY`, `SPORADIC_BUDGET_EXHAUSTED`,
  `HIGH_UTILIZATION`, `TIMING_ANOMALY`, `MESSAGE_PI_ACTIVE`, `DEADLINES_MET`.
* **Timing evidence** — a structured dictionary intended for an ISO 26262 /
  IEC 62304 / DO-178C-style safety case, with an explicit disclaimer that it
  is engineering evidence, not a certification artifact.

Anomaly detection: `mad()` is scaled by 1.4826 to be a consistent sigma
estimator; `scale()` adds a small quantization floor (0.1% of the median,
min 1 ns) so perfectly repeatable, noise-free QNX timings do not divide by
zero and hide a real spike. `cusum()` estimates sigma from a baseline window
only (estimating across two regimes would inflate sigma and hide the shift)
and requires the change to persist for `min_run` samples, so a single spike is
reported by the robust z-score detector rather than mislabelled a changepoint.

---

## 6. Autopilot

```
baseline = audit(simulate(model))
if not needs_fix(baseline): return with zero patches
loop up to max_rounds:
    candidates = _candidates(model, applied)
    for each patch:
        trial = model.clone(); apply_patch(); model_from_dict(to_dict())  # re-validate
        score = objective(audit(simulate(trial), trial))
    keep the best strictly-improving patch
    stop when misses == 0 and inversions == 0
return before/after traces, KPIs, patch set, proof
```

`objective()` weights deadline misses at 1,000,000, inversion episodes at
100,000, max lateness at 100, APS bankruptcies at 500, sporadic drops at 50,
max inversion duration at 10, jitter at 5 and `(100 - score)` at 100. Jitter
alone can never outvote a correctness fix. `needs_fix()` prevents the search
from "improving" an already-deterministic model by adding hardware — a repair
tool should leave a healthy system alone.

Patch families: clear `_NTO_CHF_FIXED_PRIORITY`, grant
`PROCMGR_AID_PRIORITY`, align/raise server and client priorities, RR/OTHER →
FIFO, raise sporadic budget, add a CPU, widen `procnto -P`, increase RR
quantum, rebalance APS budgets, mark critical threads. Every patch is
round-tripped through a TOML dict and re-validated before simulation, so a
patch can never produce an invalid QNX model.

---

## 7. Buildfile linter

`buildfile.py` parses the `[virtual=...]`, `[+script]` and file-list structure
of an `mkifs` `.build` file and applies 12 checks: `VIRTUAL`, `ARCH_64`,
`INSTRUMENTED_KERNEL`, `STARTUP_SCRIPT`, `SLOGGER2`, `LIBC`, `ENV_PATH`,
`TRACELOGGER`, `PROBE`, `PRIORITY_CEILING`, `ABILITY`,
`MODEL_PRIORITY_VS_BOOT`. The last one cross-checks model priorities against
the booted `procnto -P` ceiling — a mismatch that would silently clamp
inheritance on a real target. The shipped `chronoscope.build` scores 100/100.

---

## 8. Reporting

`render_html()` builds a single file with embedded CSS and vanilla JS:
verdict banner, KPI grid, findings table, per-thread WCET/jitter, inversion
episodes, partition usage, QNX evidence, reproduction commands, an interactive
SVG timeline (per-thread lanes, deadline markers, inversion shading, ISR
stubs), and an Autopilot before/after card. `report_json()` is machine-readable
for CI. All values are HTML-escaped. Test `test_html_is_standalone_and_offline`
asserts no external URLs.

---

## 9. Testing strategy

69 `unittest` cases across four files:

* `test_model.py` — QNX constants, validation, lossless TOML round-trip for
  all six models.
* `test_kernel.py` — PI at send time, fixed-priority channels, RR/SPORADIC,
  APS bankruptcy, ISR→IST, SMP affinity, message-passing counts, reproducible
  simulation.
* `test_pipeline.py` — trace export/import (including priority tags and process
  names), audit, autopilot, anomaly detectors, reports, buildfile lint,
  `slog2info` job reconstruction, every CLI command.
* `test_target_contract.py` — static checks on the QNX target layer: CHRONO
  wire format, QNX API inventory, brace balance, buildfile 100/100, POSIX `sh`
  syntax, and importability of C-shaped lines.

`python3 -m chronoscope verify` runs the suite from the CLI. It sets
`CHRONOSCOPE_VERIFY=1` so the CLI test that itself invokes `verify` skips in
the nested run instead of recursing forever.

---

## 10. Limitations & extension points

* The simulator models documented QNX semantics at the scheduling-decision
  level; cache effects, interrupt jitter and `procnto` implementation details
  are out of scope. **Absolute WCET must be measured on the target.**
* QNX target C sources need QNX SDP 8.0 (`qcc`) and a QNX 8.0 target;
  `target/README.md` documents the exact commands and the
  verified-vs-requires-hardware status.
* Reports are engineering evidence, not certification artifacts.
* Natural extensions: import `*.kev` binary directly, add a `slog2` streaming
  watch mode, add rate-monotonic/deadline-monotonic priority assignment as
  Autopilot patches, emit CSV for spreadsheet review, and add a GitHub Action
  that fails a PR when `audit --strict` regresses.
