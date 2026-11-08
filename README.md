# Chronoscope for QNX 8.0

> **Prove exactly when your code runs.**
> A host-based determinism workbench for QNX Neutrino 8.0 real-time systems.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![QNX](https://img.shields.io/badge/QNX-SDP%208.0-orange)
![Category](https://img.shields.io/badge/Devpost-Developer%20Tooling%20%26%20Host%20Tools-green)

**Devpost category:** Developer Tooling & Host Tools
**Repository:** <https://github.com/mimimoneydev/qnx-chronoscope>
**Codelab:** <https://mimimoneydev.github.io/qnx-chronoscope/>
**Demo video:** [`demo/chronoscope-demo.mp4`](demo/chronoscope-demo.mp4) (2:08, under the 3-minute limit) · upload pack [`demo/VIDEO.md`](demo/VIDEO.md) · public YouTube link goes in [`docs/SUBMISSION.md`](docs/SUBMISSION.md) after upload
**CI:** [`.github/workflows/ci.yml`](.github/workflows/ci.yml) — 3 OSes × Python 3.11–3.13, strict determinism gate, importers, reconciliation and generated-artifact reproducibility

---

## The problem

On a hard real-time system, "it usually finishes in 2 ms" is not an engineering
statement. What you need is: *which job missed its deadline, by how much, which
lower-priority thread was running at that instant, which QNX configuration knob
caused it, and what happens if you change that knob.* On QNX 8.0 the answer is
buried in `traceprinter` dumps, `slog2info` logs, `pidin` snapshots and a
256-level priority scheduler with message-driven priority inheritance, adaptive
partitioning, sporadic servers, runmasks and ISR/IST handoffs.

Chronoscope turns that into one reproducible loop: **model → simulate → audit →
derive a verified fix → reconcile against a real QNX 8.0 target → publish
offline timing evidence.**

It is a **host-based tool** that builds, tests, analyzes and manages software
targeting QNX 8.0 — explicitly eligible under the hackathon rules — and it ships
a **QNX 8.0 target-side probe** (C, `mkifs` buildfiles, resource manager,
`tracelogger`/`slogger2` instrumentation) so judges can see real QNX APIs in
use, not just the word "QNX" in a README.

---

## 60-second quickstart

Requires Python 3.11+ and the standard library only. No `pip install`, no
network, no QNX licence needed for the host tool.

```sh
git clone https://github.com/mimimoneydev/qnx-chronoscope.git
cd qnx-chronoscope

# 1. See a broken brake-by-wire ECU (12 priority inversions, 2 missed deadlines)
python3 -m chronoscope audit models/brake-by-wire.toml

# 2. Derive and counterfactually verify a minimal QNX configuration fix
python3 -m chronoscope fix models/brake-by-wire.toml --out /tmp/fixed.toml \
    --report /tmp/proof.html

# 3. Generate every demo artifact (HTML reports, JSON, QNX kernel trace)
python3 -m chronoscope demo --out-dir demo

# 4. Self-verification: 69 tests covering QNX scheduler semantics
python3 -m chronoscope verify
```

Open `/tmp/proof.html` — it is a **single self-contained file**, works offline
and contains an interactive per-thread timeline with deadline markers,
priority-inversion shading, ISR stubs and the before/after proof.

### The flagship result (real output, not a mockup)

```
$ python3 -m chronoscope audit models/brake-by-wire.toml
  Verdict : NON-DETERMINISTIC    Score : 57.0/100
  Jobs    : 15/15 completed      Deadline misses : 2 (13.33%)
  WCET    : 2500.0 us   Max jitter : 1800.0 us   Max lateness : 1300.0 us
  Inversions : 12   longest 1800.0 us   CPU : 55.25%
  QNX IPC : 10 MsgSend, 10 MsgReply, 0 PI boosts, 3 ISRs

  [CRITICAL] DEADLINE_MISS: 2 of 15 jobs miss their deadline
  [HIGH]     PRIORITY_INVERSION: pedal_sampler (prio 50) was STATE_REPLY on
             brake while diag_logger (prio 30) ran for 1800 us
  [HIGH]     FIXED_PRIORITY_CHANNEL: Channel 'brake' disables priority inheritance

$ python3 -m chronoscope fix models/brake-by-wire.toml
  Candidates simulated : 18    Rounds : 1    Verified fix : YES
  metric                            BEFORE         AFTER
  verdict                   NON-DETERMINISTIC DETERMINISTIC
  score                               57.0         100.0
  deadline misses                        2             0
  priority inversions                   12             0
  PATCHES
  1. channel: brake.fixed_priority True -> False
     why: Remove _NTO_CHF_FIXED_PRIORITY so the server inherits the priority
          of its highest-priority client.
     ref: C Library Reference > ChannelCreate()
```

The Autopilot does not guess. It generates ~18 QNX-semantically-valid
configuration patches, **re-simulates and re-audits every one** on the same
deterministic Neutrino scheduler, and keeps only a change that measurably
improves the determinism objective. A healthy model gets zero patches.

---

## What it does

| Capability | What you get | Command |
|---|---|---|
| **QNX 8.0 scheduler simulator** | 256 priority levels, FIFO / RR / OTHER / SPORADIC, native synchronous message passing, message-driven priority inheritance, `_NTO_CHF_FIXED_PRIORITY`, `PROCMGR_AID_PRIORITY`, mutex PI/protect, ISR→IST, adaptive partitioning (budgets, borrowing, bankruptcy, critical threads), SMP dispatch with runmasks | `simulate` |
| **Determinism audit** | Deadline misses, WCET, max lateness, response-time jitter, priority-inversion episodes with the exact blocking thread, CPU utilisation, APS bankruptcies, sporadic budget drops, ISR→IST latency, QNX IPC counts — each finding carries evidence, a recommendation and a QNX 8.0 doc reference | `audit` |
| **Determinism Autopilot** | Bounded greedy search over valid QNX configuration patches, each counterfactually verified; returns before/after traces, KPIs and a minimal patch set | `fix` |
| **Real QNX trace import** | Parses `traceprinter`, `slog2info` and `pidin` output into the same trace format; `reconcile` quantifies model-vs-target drift | `import`, `reconcile` |
| **Explainable anomaly detection** | Median/MAD robust z-score, CUSUM changepoints, EWMA control chart. Deterministic and auditable — no LLM bolted onto a scheduling problem | built into `audit` |
| **`mkifs` buildfile linting** | 12 checks: instrumented kernel, 64-bit virtual, startup script, `slogger2`, `libc.so`, `PATH`, `tracelogger`, probe, `procnto -P` ceiling, abilities/secpol, model-vs-boot priority consistency | `inspect` |
| **Offline timing evidence** | Standalone HTML + Markdown + JSON reports with structured timing evidence for an ISO 26262 / IEC 62304 / DO-178C-style safety case (explicitly *engineering evidence, not a certification artifact*) | `report` |
| **QNX 8.0 target probe** | C library, `/dev/chronoscope` resource manager, brake-by-wire demo, instrumented `mkifs` image | `target/` |

Six shipped scenarios: `brake-by-wire` (automotive, message-driven PI),
`hello-qnx` (clean baseline), `adaptive-partition` (APS bankruptcy),
`sporadic-server` (`SCHED_SPORADIC` budget), `multicore-affinity` (SMP runmasks),
`infusion-pump` (medical, mutex priority inheritance).

---

## Evidence of QNX use

> Hackathon rule: *"Your submission must explain how the project uses or
> supports QNX… Entrants should identify the relevant files, functions,
> output, or demonstration in their submission description."*

**QNX version:** QNX SDP 8.0 / QNX Everywhere (Neutrino RTOS 8.0).
**Targets:** QEMU `x86_64`, Raspberry Pi 5 `aarch64le`.
**Toolchain:** `qcc -Vgcc_ntox86_64` / `qcc -Vgcc_ntoaarch64le`, `mkifs`,
`tracelogger`, `traceprinter`, `slogger2`/`slog2info`, `pidin`.

The host tool is pure Python and *deliberately* runs on macOS/Linux/Windows so
it can be used in CI without a QNX licence — that is exactly the "host-based
tool that builds, tests, analyzes, or otherwise supports software targeting
QNX 8.0" eligibility path. The QNX-specific substance is in the model
semantics, the QNX artifact parsers, the buildfile linter and the target code:

| QNX 8.0 API / artifact | Where | What it does |
|---|---|---|
| `MsgSend` / `MsgReceive` / `MsgReply`, `STATE_SEND`/`RECEIVE`/`REPLY` | `chronoscope/kernel.py` (`_do_send`, `_try_deliver`, `_finish_server_message`); `target/demo/chronoscope_demo.c` | Models native synchronous IPC; server inherits the highest sender's priority **at send time** and does **not** revert on reply |
| `_NTO_CHF_FIXED_PRIORITY` / `ChannelCreate()` | `chronoscope/model.py`, `kernel.py::_boost`, `target/demo/chronoscope_demo.c` | The seeded defect and the winning fix |
| `PROCMGR_AID_PRIORITY` / `procmgr_ability()` / `procnto -P` | `kernel.py::_inheritance_cap`, `target/lib/chronoscope.c::chrono_grant_priority_ability`, `target/images/chronoscope.build` | 256-level priority model: unprivileged ceiling 63, privileged up to 253, reserved 254/255 for clock/IPI ISTs |
| `SCHED_FIFO` / `SCHED_RR` / `SCHED_OTHER` / `SCHED_SPORADIC`, `pthread_setschedparam` | `chronoscope/kernel.py`, `target/lib/chronoscope.c::chrono_set_fifo_priority` | Scheduling policies, RR quantum, sporadic budget/replenishment |
| Adaptive partitioning (APS) | `chronoscope/kernel.py` (`_aps_account`, bankruptcy), `models/adaptive-partition.toml` | Budgets, free-time borrowing, critical response budget, bankruptcy |
| SMP runmasks / `ThreadCtl(_NTO_TCTL_RUNMASK)` | `kernel.py` dispatch, `models/multicore-affinity.toml` | CPU affinity, per-CPU dispatch, CPU-aware inversion detection |
| `ClockTime(CLOCK_MONOTONIC)` | `target/lib/chronoscope.c::chrono_now_us` | Nanosecond monotonic timestamps |
| `slog2f()` / `slogger2` / `slog2info` | `target/lib/chronoscope.c::chrono_emit`, `chronoscope/importers.py::parse_slog2info` | `CHRONO` annotation wire format consumed by the host importer |
| `tracelogger` / `traceprinter` | `chronoscope/trace.py::to_traceprinter_text`, `importers.py::parse_traceprinter`, `target/images/chronoscope.build` | Round-trip compatible trace vocabulary (`THREAD:THRUNNING`, `KER_CALL:MsgSend()`) |
| `pidin` | `chronoscope/importers.py::parse_pidin` | Thread priority/state snapshots |
| `dispatch_create` / `resmgr_attach` / `io_devctl` / `_DEVCTL_DATA` | `target/resmgr/chronoscope_resmgr.c` | `/dev/chronoscope` resource manager with devctl commands |
| `timer_create` + `SIGEV_PULSE_INIT`, `ConnectAttach`, `MsgSendPulse` | `target/demo/chronoscope_demo.c`, `kernel.py::_do_pulse` | Periodic real-time client, priority-carrying pulses |
| `mkifs` buildfile, `procnto-smp-instr -P 64`, startup script | `target/images/chronoscope.build`, `chronoscope/buildfile.py` | Bootable instrumented image + 12-point determinism lint |
| `qcc`, `gcc_nto*`, `mkifs` | `target/Makefile`, `target/common.mk`, `target/scripts/build-target.sh` | QNX SDP 8.0 cross-build for x86_64 / aarch64le |

Run `python3 -m chronoscope inspect target/images/chronoscope.build --model
models/brake-by-wire.toml` to see the shipped image score **100.0/100** against
those checks.

**Honesty about verification:** the host tool, all six models, the 69-test
suite, the trace round-trip, the report rendering and the buildfile lint were
executed and verified on macOS with Python 3.13. The QNX target-side C sources
are written for SDP 8.0 and are accompanied by exact build/run commands, but
compiling them with `qcc` and capturing a live `traceprinter` dump requires the
QNX SDP 8.0 toolchain / QNX Everywhere VM; see
[`target/README.md`](target/README.md) for the verified-vs-requires-hardware
status table. No claim of on-target execution is made without those artifacts.

---

## Build & run instructions

### Host tool (macOS / Linux / Windows)

```sh
python3 --version                 # 3.11+ (developed & tested on 3.13)
python3 -m chronoscope version
python3 -m chronoscope verify     # 69 tests, ~5 s
```

There is nothing to install and nothing to build. All dependencies are in the
Python standard library (`tomllib`, `argparse`, `unittest`, `dataclasses`).

**Run with your own system:**

```sh
python3 -m chronoscope init --out my-ecu.toml
# edit my-ecu.toml: threads, priorities, periods, deadlines, channels, partitions
python3 -m chronoscope audit my-ecu.toml
python3 -m chronoscope report my-ecu.toml --out my-ecu.html
```

### QNX 8.0 target (QEMU x86_64 — QNX SDP 8.0 / QNX Everywhere)

```sh
# On a machine with QNX SDP 8.0 installed (qcc, mkifs on PATH via qnxsdp-env.sh)
cd target
./scripts/build-target.sh x86_64          # builds lib + demo + resmgr + IFS
./scripts/run-qemu.sh                     # boots target/images/chronoscope.ifs
```

Inside the QNX 8.0 target:

```sh
chronoscope_demo --fixed-priority --iterations 10   # reproduce the defect
chronoscope_demo --inherit       --iterations 10   # with QNX priority inheritance
slog2info -b chronoscope > /tmp/chronoscope.slog
tracelogger -d1 -f /tmp/chronoscope.kev -n 8 -s 3
traceprinter -f /tmp/chronoscope.kev > /tmp/qnx-kernel-trace.txt
pidin -F '%P %T %n %p %S' > /tmp/pidin.txt
```

Copy those files back to the host and close the loop:

```sh
python3 -m chronoscope simulate models/brake-by-wire.toml --out /tmp/simulated.json
python3 -m chronoscope import /tmp/qnx-kernel-trace.txt --format traceprinter --audit
python3 -m chronoscope import /tmp/chronoscope.slog --format slog2 --audit
python3 -m chronoscope reconcile /tmp/simulated.json /tmp/qnx-kernel-trace.txt
```

**No target yet?** `examples/traces/` ships synthetic `traceprinter`,
`slog2info`, bare `CHRONO` and `pidin` samples in the exact QNX tool formats,
clearly labelled as simulations. They are regenerated by
`python3 examples/traces/generate.py`. Try the whole loop offline:

```sh
python3 -m chronoscope import examples/traces/traceprinter.txt --format traceprinter --audit
python3 -m chronoscope import examples/traces/slog2info.txt --format slog2 --audit
python3 -m chronoscope reconcile examples/traces/simulated-trace.json \
    examples/traces/traceprinter.txt
```

The first command reproduces the flagship **57.0/100, 2 deadline misses, 12
inversions** verdict from a `traceprinter`-shaped file rather than from a TOML
model — a round-trip proof that the simulated and measured paths share one
`Trace` format. See [`examples/traces/README.md`](examples/traces/README.md)
for the honesty note about what these samples are and are not.

### Raspberry Pi 5 (aarch64le)

See [`target/scripts/run-rpi5.md`](target/scripts/run-rpi5.md). Build with
`./scripts/build-target.sh aarch64le` (or `make CPU=aarch64le
GCC_VERSION=12.0 all`); note that `chronoscope.build` must be adapted from
`[virtual=x86_64,elf]` / `startup-x86` to the Pi 5 aarch64le startup.

---

## CLI reference

| Command | Purpose |
|---|---|
| `init` | Write a starter TOML system model |
| `simulate MODEL` | Run the QNX 8.0 discrete-event scheduler; `--out trace.json` |
| `audit MODEL` | Determinism verdict, KPIs, findings, QNX references |
| `fix MODEL` | Derive + verify a minimal QNX configuration fix; `--out`, `--report`, `--json` |
| `report MODEL` | Standalone HTML / Markdown / JSON timing evidence |
| `import ARTIFACT` | Parse `traceprinter` / `slog2info` / `pidin` output; `--format {auto,traceprinter,slog2,pidin}`, `--audit` |
| `inspect BUILD` | Lint a `mkifs` `.build` file; `--model` for priority cross-checks |
| `reconcile SIMULATED.json ARTIFACT` | Quantify simulated-vs-measured drift |
| `demo` | Generate all demo artifacts at once |
| `verify` | Run the bundled 69-test self-verification suite |
| `version` | Version / QNX target / toolchain info |

---

## Architecture

```
  models/*.toml            QNX 8.0 target artifacts        target/*.c, *.build
  (declarative system)     (traceprinter/slog2info/pidin)  (qcc / mkifs probe)
        │                            │                            │
        ▼                            ▼                            │
  ┌───────────┐              ┌─────────────┐                     │
  │  kernel   │              │  importers  │◄────────────────────┘
  │ simulate  │              │  reconcile  │
  └─────┬─────┘              └──────┬──────┘
        │        Trace (shared currency)     │
        ├──────────────────┬───────────────┤
        ▼                  ▼               ▼
     audit ─────────► autopilot        anomaly
   (findings/KPIs)   (verified fix)   (z/CUSUM/EWMA)
        │                  │
        └────────┬─────────┘
                 ▼
     report.py → standalone HTML / Markdown / JSON
                 buildfile.py → mkifs determinism checks
```

`Trace` is the single shared currency: simulation, real-trace import, audit,
autopilot, reconciliation and reporting all consume the same object. Reports
are dependency-free and offline-capable; the HTML has no external URLs.

---

## Codelab (Best Codelab / Tutorial bonus prize)

A complete, hands-on, ~40-minute codelab authored during the Submission Period:

**"Find and fix a priority inversion on QNX 8.0"**
<https://mimimoneydev.github.io/qnx-chronoscope/>

Markdown source: [`codelab/README.md`](codelab/README.md). It teaches the QNX
8.0 priority model, message-driven priority inheritance, `_NTO_CHF_FIXED_PRIORITY`,
`PROCMGR_AID_PRIORITY`, `procnto -P`, `tracelogger`/`traceprinter` capture,
`slogger2`, `mkifs` boot configuration and model/target reconciliation — with
copy-pasteable commands, expected output, checkpoints and a knowledge check.

---

## Testing & self-verification

```sh
python3 -m chronoscope verify
# Ran 69 tests ... OK (skipped=1)
# Self-verification: 69 tests, 0 failures, 0 errors, 1 re-entrancy guard skipped

python3 -m unittest discover -s tests   # all 69 cases, no skips
```

`verify` runs the suite from the CLI; the one skip is a deliberate recursion
guard on the test that itself invokes `verify` in a subprocess. Running the
suite directly executes every case.

The suite covers QNX constants (256 levels, reserved 254/255, ceiling 63),
scheduler semantics (PI at send time, fixed-priority channels, sporadic budget
drops, APS bankruptcy, ISR→IST, SMP affinity, reproducibility), TOML round-trips,
`traceprinter` export/import with priority tags and process names, audit
findings, autopilot counterfactual verification, anomaly detectors, offline HTML,
buildfile linting and every CLI command.

---

## Scope & limitations (stated plainly)

* The simulator is a **discrete-event model** of documented QNX Neutrino 8.0
  scheduling semantics, not a cycle-accurate emulator of `procnto`. It is
  intended for configuration exploration and regression detection; absolute
  WCET must still be measured on the target with `traceprinter`.
* Reports are **engineering timing evidence**, not a certification artifact or
  a claim of ISO 26262 / IEC 62304 / DO-178C compliance.
* The QNX target C code requires QNX SDP 8.0 to compile (`qcc` is not available
  in a generic CI environment). Everything host-side runs anywhere with Python.
* The Anomaly detectors are deliberately small, deterministic and explainable;
  Chronoscope does not send timing data to any external service.

---

## Submission

* Category: **Developer Tooling & Host Tools**
* Competing for: Grand Prize, Best of Category, **Best Codelab / Tutorial**,
  **Best use of QNX-specific Technologies**, Community Choice.
* Full requirements-to-artifact compliance matrix, Devpost submission text and
  the "Evidence of QNX use" block: [`docs/SUBMISSION.md`](docs/SUBMISSION.md).
* Architecture notes: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## License

MIT — see [`LICENSE`](LICENSE). QNX is a trademark of BlackBerry Limited; this
project is an independent submission and is not affiliated with or endorsed by
BlackBerry/QNX.
