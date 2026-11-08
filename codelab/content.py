"""Single source of truth for the QNX 8.0 Chronoscope codelab.

``build.py`` renders this into both:

* ``codelab/README.md``  -- readable in the repository / on GitHub
* ``docs/index.html``    -- GitHub Pages codelab URL for the Devpost bonus field

Keeping one source means the two published copies cannot disagree.
"""

from __future__ import annotations

TITLE = "Find and fix a priority inversion on QNX 8.0"
SUBTITLE = ("A hands-on codelab: model a brake-by-wire ECU, prove its deadlines "
            "are unsafe, derive a verified QNX configuration fix, then reconcile "
            "against a real QNX 8.0 target trace.")
DURATION = "~40 minutes"
LEVEL = "Intermediate"
AUTHORED = "November 2026 (QNX Global Hackathon Submission Period)"

OBJECTIVES = [
    "Describe a QNX Neutrino 8.0 real-time system declaratively (priorities, "
    "scheduling policies, channels, partitions, deadlines).",
    "Reproduce an unbounded priority inversion caused by "
    "`_NTO_CHF_FIXED_PRIORITY` and read the timing evidence.",
    "Explain QNX message-driven priority inheritance, `PROCMGR_AID_PRIORITY` "
    "and the `procnto -P` unprivileged priority ceiling.",
    "Derive a minimal configuration fix that is **counterfactually verified** "
    "by re-simulation, not suggested by guesswork.",
    "Capture `traceprinter` / `slog2info` / `pidin` output on QNX 8.0 (QEMU or "
    "Raspberry Pi 5) and reconcile it with the model.",
    "Lint a `mkifs` buildfile for boot-time determinism hazards.",
]

PREREQS = [
    "A host running macOS, Linux or Windows with **Python 3.11+** (no pip "
    "packages, no network).",
    "A clone of `https://github.com/mimimoneydev/qnx-chronoscope`.",
    "For Step 7 and the on-target part of Step 8 only: **QNX SDP 8.0 / QNX "
    "Everywhere** with `qcc`, `mkifs`, `tracelogger`, `traceprinter` and "
    "`slogger2`, plus a QEMU x86_64 target or a Raspberry Pi 5 `aarch64le` "
    "image. Steps 1-6 and 9-11 need no QNX toolchain at all.",
]


def b(text: str) -> dict:
    return {"type": "text", "text": text}


def code(lang: str, body: str, caption: str = "") -> dict:
    return {"type": "code", "lang": lang, "code": body.strip("\n"),
            "caption": caption}


def out(body: str, caption: str = "Expected output") -> dict:
    return {"type": "output", "code": body.strip("\n"), "caption": caption}


def note(text: str, kind: str = "note") -> dict:
    return {"type": "note", "kind": kind, "text": text}


def items(*xs: str) -> dict:
    return {"type": "list", "items": list(xs)}


def check(text: str) -> dict:
    return {"type": "checkpoint", "text": text}


STEPS = [
    {
        "id": "setup",
        "title": "Set up the workspace",
        "minutes": 2,
        "blocks": [
            b("Clone the repository and confirm the host tool is healthy. "
              "Everything in Steps 0-6 runs on an ordinary laptop with the "
              "Python standard library only."),
            code("sh", """
git clone https://github.com/mimimoneydev/qnx-chronoscope.git
cd qnx-chronoscope
python3 -m chronoscope version
python3 -m chronoscope verify
""", "Terminal"),
            out("""
Chronoscope 1.0.0 -- QNX 8.0 determinism workbench
...
Ran 69 tests in ~5s
OK
Self-verification: 69 tests, 0 failures, 0 errors
""", "Expected output (abbreviated)"),
            note("`verify` runs the bundled scheduler-semantics suite. If it "
                 "fails, stop here and fix the environment before continuing.",
                 "note"),
            check("`python3 -m chronoscope verify` reports 0 failures and 0 errors."),
        ],
        "refs": ["README.md > Quickstart"],
    },
    {
        "id": "model",
        "title": "Describe a QNX 8.0 system in TOML",
        "minutes": 5,
        "blocks": [
            b("A Chronoscope model is a declarative description of the "
              "threads, priorities, deadlines and IPC topology you would "
              "otherwise spread across C source, a `mkifs` buildfile and "
              "tribal knowledge. Open `models/brake-by-wire.toml`."),
            code("toml", """
name = "brake-by-wire"
horizon_us = 20000
cpus = 1
unprivileged_ceiling = 63       # procnto -P 64

[[channels]]
name = "brake"
server = "brake_server"
fixed_priority = true           # _NTO_CHF_FIXED_PRIORITY -- the seeded defect

[[threads]]
name = "pedal_sampler"
priority = 50
policy = "FIFO"
role = "worker"
partition = "system"
jobs = [
    { name = "pedal_cycle", period_us = 2000, offset_us = 0,
      deadline_us = 1200, critical = true, steps = [
        { op = "compute", us = 200 },
        { op = "send", channel = "brake", service_us = 400 },
        { op = "compute", us = 100 },
    ] },
]
""", "models/brake-by-wire.toml (abridged)"),
            items(
                "**256 priority levels.** `0` is idle; `1..62` are unprivileged; "
                "`63..253` require `PROCMGR_AID_PRIORITY`; `254` is reserved for "
                "clock/interrupt service threads and `255` for per-CPU IPI ISTs. "
                "The default `procnto -P` ceiling is 63.",
                "**Policies.** `FIFO`, `RR`, `OTHER` and `SPORADIC` are all "
                "modelled, including RR quantum and sporadic budget "
                "replenishment.",
                "**Jobs.** A periodic job has a period, offset, relative "
                "deadline and a script of steps (`compute`, `send`, `lock`, "
                "`sleep`, `pulse`, `yield`, `interrupt_wait`, `set_priority`).",
                "**Channels.** A `send` step blocks in `STATE_SEND` / "
                "`STATE_REPLY` until the server completes `MsgReply()`.",
            ),
            b("The full model also declares `brake_server` at priority 20, "
              "a `diag_logger` at priority 30 that hogs the CPU, a "
              "`wheel_speed_ist` at priority 60 and a `wheel_tick` interrupt. "
              "That is the classic recipe for unbounded priority inversion."),
            code("sh", """
python3 -m chronoscope init --out my-ecu.toml   # starter template to experiment with
""", "Optional: start your own model"),
            check("You can point to the file that sets `fixed_priority = true` "
                  "and explain which thread is the high-priority client."),
        ],
        "refs": [
            "QNX 8.0 System Architecture > Scheduling priority",
            "QNX 8.0 C Library Reference > ChannelCreate()",
        ],
    },
    {
        "id": "reproduce",
        "title": "Reproduce and measure the defect",
        "minutes": 4,
        "blocks": [
            b("Run the audit. It simulates the model on a deterministic "
              "discrete-event implementation of the Neutrino scheduler, then "
              "converts the resulting trace into a verdict, KPIs and findings."),
            code("sh", "python3 -m chronoscope audit models/brake-by-wire.toml",
                 "Terminal"),
            out("""
====================================================================
  CHRONOSCOPE DETERMINISM AUDIT  brake-by-wire
====================================================================
  Verdict : NON-DETERMINISTIC    Score : 57.0/100
  Jobs    : 15/15 completed    Deadline misses : 2 (13.33%)
  WCET    : 2500.0 us    Max jitter : 1800.0 us    Max lateness : 1300.0 us
  Inversions : 12    longest 1800.0 us    CPU : 55.25%
  QNX IPC : 10 MsgSend, 10 MsgReply, 0 PI boosts, 3 ISRs
--------------------------------------------------------------------
  FINDINGS
  [CRITICAL] DEADLINE_MISS: 2 of 15 jobs miss their deadline
  [HIGH    ] PRIORITY_INVERSION: pedal_sampler (prio 50) was STATE_REPLY
             on brake while diag_logger (prio 30) ran for 1800 us.
  [HIGH    ] FIXED_PRIORITY_CHANNEL: Channel 'brake' disables priority
             inheritance
""", "Expected output (abridged)"),
            b("Read the findings, not just the verdict. Each one carries "
              "**evidence** (which thread, which priority, which duration), a "
              "**recommendation** and a **QNX 8.0 documentation reference**. "
              "The inversion finding names `diag_logger` at priority 30 "
              "running while `pedal_sampler` at priority 50 is "
              "`STATE_REPLY`-blocked on the brake channel — that is the "
              "unbounded blocking chain."),
            note("`--strict` turns the audit into a CI gate: it exits 1 when "
                 "deadline misses remain. Try "
                 "`python3 -m chronoscope audit models/hello-qnx.toml --strict` "
                 "for the passing case.", "tip"),
            check("You can state the WCET, max lateness, number of inversions "
                  "and which thread caused the longest one."),
        ],
        "refs": ["QNX 8.0 System Architecture > Priority inheritance and messages"],
    },
    {
        "id": "semantics",
        "title": "Understand the QNX 8.0 semantics behind it",
        "minutes": 4,
        "blocks": [
            b("QNX Neutrino uses **synchronous native message passing**. "
              "A client calling `MsgSend()` blocks until the server calls "
              "`MsgReply()`. Because the server executes on behalf of the "
              "client, the microkernel applies **message-driven priority "
              "inheritance**: at *send* time, if the sender's priority is "
              "higher than the server's, the server is boosted to the "
              "sender's priority."),
            items(
                "The boost happens at `MsgSend()`, not at `MsgReceive()`.",
                "The server priority **does not revert** on `MsgReply()`; "
                "it is lowered when it next receives a lower-priority message.",
                "Pulses carry the priority of the sender (`SIGEV_PULSE_"
                "PRIO_INHERIT` is used in the target demo).",
                "`_NTO_CHF_FIXED_PRIORITY` on `ChannelCreate()` disables this "
                "inheritance entirely — useful for a deliberately fixed-priority "
                "server, catastrophic for a safety-critical channel.",
                "Without `PROCMGR_AID_PRIORITY`, inherited priority is clamped "
                "to the highest unprivileged priority (`procnto -P` ceiling "
                "minus one). Grant the ability with `procmgr_ability()`.",
            ),
            code("c", """
/* target/demo/chronoscope_demo.c -- the seeded defect, on real QNX 8.0 */
g_demo.server_chid = ChannelCreate(g_demo.cfg.fixed_priority
                                    ? _NTO_CHF_FIXED_PRIORITY : 0);

/* target/lib/chronoscope.c -- allow inheritance above procnto -P */
abilities[0].ability = PROCMGR_AID_PRIORITY;
abilities[0].flags   = PROCMGR_ADV_NONROOT | PROCMGR_AOP_ALLOW
                     | PROCMGR_AINHERIT_YES;
procmgr_ability((pid_t)0, abilities, flags);
""", "The same two knobs in QNX C code"),
            note("This is why simply raising `brake_server`'s base priority "
                 "is not always the right fix: a fixed-priority channel still "
                 "cannot inherit from an even higher-priority client (the "
                 "wheel-speed IST at priority 60).", "warning"),
            check("You can explain why the server ran at priority 20 while a "
                  "priority-50 client was blocked, and what QNX normally does "
                  "to prevent that."),
        ],
        "refs": [
            "QNX 8.0 System Architecture > Priority inheritance and messages",
            "QNX 8.0 C Library Reference > procmgr_ability(), ChannelCreate()",
            "QNX 8.0 Utilities Reference > procnto (-P)",
        ],
    },
    {
        "id": "fix",
        "title": "Derive and verify a minimal fix (Autopilot)",
        "minutes": 4,
        "blocks": [
            b("Most timing tools stop at \"you missed a deadline\". "
              "Chronoscope treats the QNX system model as a search space: it "
              "generates semantically valid configuration patches, "
              "**re-simulates and re-audits every candidate**, and keeps only "
              "a change that measurably improves the determinism objective."),
            code("sh", """
python3 -m chronoscope fix models/brake-by-wire.toml \\
    --out /tmp/fixed.toml --report /tmp/proof.html
""", "Terminal"),
            out("""
====================================================================
  CHRONOSCOPE DETERMINISM AUTOPILOT
====================================================================
  Candidates simulated : 18    Rounds : 1    Verified fix : YES
  metric                            BEFORE         AFTER
  verdict                   NON-DETERMINISTIC DETERMINISTIC
  score                               57.0         100.0
  deadline misses                        2             0
  priority inversions                   12             0
--------------------------------------------------------------------
  PATCHES
  1. channel: brake.fixed_priority True -> False
     why: Remove _NTO_CHF_FIXED_PRIORITY so the server inherits the
          priority of its highest-priority client.
     ref: C Library Reference > ChannelCreate()
""", "Expected output (abridged)"),
            b("Candidate patch families include: clearing "
              "`_NTO_CHF_FIXED_PRIORITY`, granting `PROCMGR_AID_PRIORITY`, "
              "raising server/client priorities, switching RR/OTHER to FIFO, "
              "increasing `SCHED_SPORADIC` budget, adding a CPU, widening the "
              "`procnto -P` ceiling, increasing the RR quantum, rebalancing "
              "APS budgets and marking critical threads. The objective is "
              "dominated by deadline misses (1,000,000x) and inversion "
              "episodes (100,000x), so cosmetic jitter wins cannot outvote a "
              "correctness fix."),
            note("A healthy configuration is left alone: running `fix` on "
                 "`models/hello-qnx.toml` returns zero patches. The Autopilot "
                 "is a repair tool, not a tuning tool.", "tip"),
            code("sh", """
python3 -m chronoscope audit /tmp/fixed.toml --strict
# exit 0 -- every modelled job now meets its deadline
""", "Verify the patched model independently"),
            check("The proof shows 0 deadline misses and 0 inversions after "
                  "the patch, and you can name the single QNX flag that changed."),
        ],
        "refs": ["chronoscope/autopilot.py > objective(), _candidates()"],
    },
    {
        "id": "evidence",
        "title": "Publish offline timing evidence",
        "minutes": 3,
        "blocks": [
            b("A safety argument needs an artifact, not a terminal scrollback. "
              "`report` renders a single self-contained HTML file with an "
              "interactive per-thread timeline, deadline markers, "
              "priority-inversion shading, ISR stubs and the before/after proof."),
            code("sh", """
python3 -m chronoscope report models/brake-by-wire.toml \\
    --fix --out /tmp/brake-by-wire.html --markdown /tmp/brake-by-wire.md

python3 -m chronoscope audit models/brake-by-wire.toml --json /tmp/audit.json
""", "Terminal"),
            items(
                "**No external URLs.** Open the HTML offline, in an air-gapped "
                "review, or inside a QNX build container.",
                "**Structured timing evidence** in JSON: verdict, per-job "
                "release/response/deadline, per-thread WCET and jitter, "
                "inversion episodes, QNX IPC counts and APS/ISR metrics.",
                "**Explicit scope statement**: this is engineering evidence "
                "for an ISO 26262 / IEC 62304 / DO-178C-style safety case, "
                "*not* a certification artifact or a claim of compliance.",
                "`--strict` makes an audit a pass/fail CI gate; `--json` feeds "
                "regression dashboards.",
            ),
            code("sh", """
python3 -m chronoscope demo --out-dir demo
# writes an HTML report + JSON per model, proof.html, proof.json,
# a traceprinter-compatible qnx-kernel-trace.txt and a README
""", "Generate every artifact at once"),
            check("`/tmp/brake-by-wire.html` opens with no network access and "
                  "shows the before/after timeline."),
        ],
        "refs": ["chronoscope/report.py > render_html(), report_json()"],
    },
    {
        "id": "target",
        "title": "Reproduce it on a real QNX 8.0 target",
        "minutes": 6,
        "blocks": [
            b("Now close the loop on QNX itself. With **QNX SDP 8.0 / QNX "
              "Everywhere** installed, source `qnxsdp-env.sh` and cross-build "
              "the target probe with `qcc`."),
            code("sh", """
cd target
./scripts/build-target.sh x86_64       # lib + demo + resmgr + mkifs image
./scripts/run-qemu.sh                  # boots the QNX 8.0 IFS in QEMU
""", "Host terminal (QNX SDP 8.0)"),
            code("text", """
[virtual=x86_64,elf] .bootstrap = {
    startup-x86
    procnto-smp-instr -P 64
}
[+script] startup-script = {
    PATH=/proc/boot:/usr/bin:/bin
    slogger2 -d -s 256k &
    tracelogger -d1 -f /tmp/chronoscope.kev -n 8 &
    /usr/bin/chronoscope_resmgr &
    waitfor /dev/chronoscope 5
    /usr/bin/chronoscope_demo --inherit --iterations 10
}
""", "target/images/chronoscope.build (abridged)"),
            b("`procnto-smp-instr` is required for `tracelogger`; `-P 64` "
              "sets the unprivileged priority ceiling that the model mirrors. "
              "The demo uses `ClockTime(CLOCK_MONOTONIC)`, "
              "`timer_create()`+`SIGEV_PULSE_INIT`, `ChannelCreate()`, "
              "`ConnectAttach()`, `MsgSend()`/`MsgReceive()`/`MsgReply()`, "
              "`pthread_setschedparam(SCHED_FIFO, ...)` and `slog2f()`."),
            code("sh", """
# Inside the QNX 8.0 target
chronoscope_demo --fixed-priority --iterations 10   # reproduce the defect
chronoscope_demo --inherit       --iterations 10   # QNX priority inheritance

slog2info -b chronoscope > /tmp/chronoscope.slog
tracelogger -d1 -f /tmp/chronoscope.kev -n 8 -s 3
traceprinter -f /tmp/chronoscope.kev > /tmp/qnx-kernel-trace.txt
pidin -F '%P %T %n %p %S' > /tmp/pidin.txt
""", "QNX 8.0 target shell"),
            b("The `CHRONO` annotations emitted by `slog2f()` carry job "
              "release/completion, `MsgSend`/`MsgReply`, priority boosts and "
              "the channel flag, so the host importer can reconstruct the "
              "same `Trace` object from a real target."),
            note("Raspberry Pi 5: build with "
                 "`./scripts/build-target.sh aarch64le` and adapt the "
                 "buildfile's `[virtual=...]` / startup to the Pi 5 board "
                 "support package. See `target/scripts/run-rpi5.md`.", "note"),
            note("No QNX hardware handy? Steps 1-6 and 9-11 are fully runnable "
                 "on a laptop, and `python3 -m chronoscope demo` writes a "
                 "traceprinter-compatible trace you can use in Step 8.", "tip"),
            check("You can name the tool that produces the kernel event file "
                  "(`tracelogger`) and the one that renders it (`traceprinter`)."),
        ],
        "refs": [
            "QNX 8.0 Utilities Reference > tracelogger, traceprinter, pidin, slog2info",
            "QNX 8.0 Programmer's Guide > Resource managers",
            "target/README.md > QNX 8.0 API map",
        ],
    },
    {
        "id": "reconcile",
        "title": "Import real traces and reconcile",
        "minutes": 4,
        "blocks": [
            b("Chronoscope speaks QNX tool output directly. The simulated "
              "trace uses the same `traceprinter` vocabulary "
              "(`THREAD :THRUNNING`, `KER_CALL :MsgSend()`, "
              "`PROCESS :PROCCREATE`) so it can be round-tripped through the "
              "importer — a useful way to verify the parser without hardware."),
            code("sh", """
python3 -m chronoscope simulate models/brake-by-wire.toml \\
    --out /tmp/simulated.json --traceprinter /tmp/simulated-trace.txt

python3 -m chronoscope import /tmp/qnx-kernel-trace.txt \\
    --format traceprinter --audit
python3 -m chronoscope import /tmp/chronoscope.slog --format slog2 --audit
python3 -m chronoscope import /tmp/pidin.txt --format pidin

python3 -m chronoscope reconcile /tmp/simulated.json /tmp/qnx-kernel-trace.txt
""", "Host terminal"),
            out("""
MODEL / TARGET RECONCILIATION
  Verdict : DRIFT DETECTED
  rows    : 15 jobs
  ...
""", "Reconciliation output (shape)"),
            b("`reconcile` compares per-job release, response and deadline "
              "outcomes between the simulated trace and the measured QNX "
              "trace. `ALIGNED` means the model predicts the target; "
              "`DRIFT DETECTED` means your WCET assumptions, priority "
              "assignments or boot configuration need revisiting. This is the "
              "step that turns a nice simulation into a tool you can trust."),
            note("Priority tags (`prio:<n>`) and process names survive "
                 "export/import, so inversion analysis works on imported "
                 "traces too. On SMP targets, blocked segments are tagged with "
                 "the CPU the thread last ran on, so a low-priority thread on "
                 "another CPU is not falsely blamed.", "tip"),
            check("You can import the demo's `qnx-kernel-trace.txt` and get a "
                  "non-deterministic verdict with the same missed deadlines."),
        ],
        "refs": ["chronoscope/importers.py > parse_traceprinter(), reconcile()"],
    },
    {
        "id": "buildfile",
        "title": "Lint the boot image for determinism hazards",
        "minutes": 3,
        "blocks": [
            b("Determinism is decided before `main()` runs. `inspect` parses a "
              "`mkifs` `.build` file and applies 12 checks, including "
              "cross-checking model priorities against the `procnto -P` boot "
              "ceiling."),
            code("sh", """
python3 -m chronoscope inspect target/images/chronoscope.build \\
    --model models/brake-by-wire.toml
""", "Terminal"),
            out("""
QNX 8.0 BUILDFILE DETERMINISM CHECK  (100.0/100)
  [PASS] VIRTUAL                    [PASS] ARCH_64
  [PASS] INSTRUMENTED_KERNEL        [PASS] STARTUP_SCRIPT
  [PASS] SLOGGER2                   [PASS] LIBC
  [PASS] ENV_PATH                   [PASS] TRACELOGGER
  [PASS] PROBE                      [PASS] PRIORITY_CEILING
  [PASS] ABILITY                    [PASS] MODEL_PRIORITY_VS_BOOT
""", "Expected output (abridged)"),
            b("Try removing `procnto-smp-instr` from a copy of the buildfile "
              "and re-running the check: `INSTRUMENTED_KERNEL` fails, because "
              "`tracelogger` cannot work without the instrumented microkernel. "
              "Try setting a model priority above the boot ceiling to see "
              "`MODEL_PRIORITY_VS_BOOT` fail."),
            check("You can explain why shipping a non-instrumented kernel "
                  "breaks the whole measurement workflow."),
        ],
        "refs": ["chronoscope/buildfile.py > lint_buildfile()"],
    },
    {
        "id": "quiz",
        "title": "Knowledge check",
        "minutes": 2,
        "blocks": [
            b("Answers are in the expandable section at the end of the page."),
            items(
                "**Q1.** On QNX 8.0, when does message-driven priority "
                "inheritance boost a server — at `MsgSend()` or `MsgReply()`?",
                "**Q2.** Does a server's priority revert when it calls "
                "`MsgReply()`?",
                "**Q3.** What does `_NTO_CHF_FIXED_PRIORITY` do, and why did "
                "clearing it fix the brake-by-wire ECU?",
                "**Q4.** Why might granting `PROCMGR_AID_PRIORITY` still be "
                "necessary after clearing the fixed-priority flag?",
                "**Q5.** Which priorities are reserved for QNX interrupt "
                "service threads, and what is the default `procnto -P` ceiling?",
                "**Q6.** Why must the image boot `procnto-smp-instr` rather "
                "than plain `procnto`?",
            ),
        ],
        "refs": [],
    },
    {
        "id": "next",
        "title": "Where to go next",
        "minutes": 2,
        "blocks": [
            b("The repository ships five more models, each exercising a "
              "different QNX 8.0 mechanism:"),
            items(
                "`models/hello-qnx.toml` — a clean baseline (score 100/100, "
                "one message-driven priority boost).",
                "`models/adaptive-partition.toml` — APS budget exhaustion and "
                "bankruptcy on a starved safety partition.",
                "`models/sporadic-server.toml` — `SCHED_SPORADIC` budget "
                "enforcement and low-priority degradation.",
                "`models/multicore-affinity.toml` — SMP runmasks and "
                "CPU-aware inversion detection.",
                "`models/infusion-pump.toml` — mutex priority inheritance in a "
                "medical device.",
            ),
            b("Try `python3 -m chronoscope demo --out-dir demo` to audit all "
              "of them, or `python3 -m chronoscope audit <model> --strict` in "
              "CI. When a real target disagrees with the model, use "
              "`reconcile` to find out where."),
            note("Troubleshooting: (1) `ModuleNotFoundError: chronoscope` "
                 "means you are not in the repository root. (2) `ModelError: "
                 "priority 0 is reserved for idle` — use 1..253. (3) If "
                 "`reconcile` reports drift, check `procnto -P` in the "
                 "buildfile matches `unprivileged_ceiling` in the model. "
                 "(4) `traceprinter` needs `procnto-smp-instr`.", "note"),
            b("**References:** QNX 8.0 documentation at "
              "https://www.qnx.com/developers/docs/8.0/ — System "
              "Architecture (scheduling, priority inheritance), C Library "
              "Reference (`ChannelCreate`, `procmgr_ability`, `MsgSend`), "
              "Utilities Reference (`procnto`, `tracelogger`, `traceprinter`, "
              "`pidin`, `slog2info`), Adaptive Partitioning User's Guide and "
              "the Programmer's Guide (Multicore Processing, Resource "
              "Managers). Sample projects: https://gitlab.com/qnx/projects."),
        ],
        "refs": [],
    },
]

ANSWERS = [
    "At `MsgSend()` time, when a higher-priority client sends to a "
    "lower-priority server. The microkernel boosts the server immediately so "
    "it can run on the client's behalf.",
    "No. The elevated priority persists; it is lowered when the server next "
    "receives a message from a lower-priority client.",
    "It disables inheritance for that channel. With it set, `brake_server` "
    "stayed at priority 20 while the priority-50 `pedal_sampler` was "
    "`STATE_REPLY`-blocked, so `diag_logger` (priority 30) could run for "
    "1800 us. Clearing it lets the server inherit priority 50 and preempt "
    "the logger.",
    "Because without `PROCMGR_AID_PRIORITY`, inherited priority is clamped to "
    "the highest unprivileged priority (`procnto -P` ceiling minus one, i.e. "
    "62 by default). A client above that ceiling still cannot be inherited.",
    "254 for clock/in-kernel interrupt service threads and 255 for "
    "per-processor IPI ISTs. The default unprivileged ceiling is 63 "
    "(`procnto -P 63`), i.e. priorities 63..253 are privileged.",
    "`tracelogger` and the kernel event trace require the instrumented "
    "microkernel. Without it there is no event file for `traceprinter`, so "
    "the entire measurement and reconciliation workflow has no input.",
]
