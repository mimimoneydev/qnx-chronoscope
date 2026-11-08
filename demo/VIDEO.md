# Demo video — upload pack

**File:** `demo/chronoscope-demo.mp4` (H.264 1920×1080 / AAC, ~2 minutes — under
the Devpost 3-minute limit).

The video is **original**: every terminal block is live CLI output captured at
build time, every report panel is a screenshot of the generated standalone
HTML, every code excerpt is copied from this repository, and the narration is
generated with the built-in macOS system voice. No third-party trademarks,
logos, stock footage, fonts or music. The QNX name appears only as plain text
to identify the target platform.

Regenerate with `python3 demo/build_video.py` (see the header of that script for
the macOS/Chrome/ffmpeg prerequisites). The MP4 is checked in, so nobody needs
to run it.

---

## YouTube upload (the one manual step)

1. Upload `demo/chronoscope-demo.mp4` to YouTube, visibility **Public**.
2. Use the title and description below.
3. Paste the public URL into the Devpost **video** field and into
   `docs/SUBMISSION.md` / `README.md` where the placeholder appears.
4. Confirm the video is under 3 minutes and plays with audio.

### Title (≤ 100 characters)

```
Chronoscope for QNX 8.0 — Prove Exactly When Your Code Runs (Devpost Demo)
```

### Description

```
Chronoscope is a host-based determinism workbench for QNX Neutrino 8.0.
It models a real-time system in TOML, simulates the QNX 8.0 scheduler, audits
every deadline, derives a counterfactually verified QNX configuration fix,
imports real traceprinter / slog2info / pidin artifacts and publishes a
standalone offline HTML timing report.

Demo (all output is real):
  00:00  The problem and the reproducible loop
  00:20  Audit a brake-by-wire ECU: NON-DETERMINISTIC, 57/100, 2 deadline
         misses, 12 priority inversions
  00:40  Offline interactive timing report
  01:00  Why _NTO_CHF_FIXED_PRIORITY disables QNX message-driven priority
         inheritance
  01:20  Determinism Autopilot: 18 QNX configurations re-simulated and
         re-audited, one verified patch
  01:40  Before/after proof: 57 → 100, 2 → 0 misses, 12 → 0 inversions
  02:00  QNX 8.0 target C code, mkifs image, trace import, reconciliation,
         codelab, tests and MIT licence

Repository: https://github.com/mimimoneydev/qnx-chronoscope
Codelab:    https://mimimoneydev.github.io/qnx-chronoscope/
License:    MIT

Built for the QNX Global Hackathon 2026 — Developer Tooling & Host Tools.
Python 3.11+ standard library only; no pip install and no QNX licence are
needed to run the host tool. Try the 60-second quickstart in the README.

Note: the QNX target-side C sources require QNX SDP 8.0 to compile and a QNX
8.0 target to capture live traces. target/README.md states exactly what was
executed here and what requires hardware. No on-target execution is claimed
without captured artifacts.
```

### Tags

```
QNX, QNX 8.0, QNX Everywhere, real-time, RTOS, determinism, priority inversion,
traceprinter, slogger2, mkifs, embedded, automotive, DevOps, Devpost, hackathon
```

---

## Narration transcript (original, spoken by the system voice)

1. **Chronoscope for QNX 8.0.** Prove exactly when your code runs. A host-based
   determinism workbench for hard real-time systems.
2. On a hard real-time OS, a missed deadline is a defect, not a slowdown.
   Finding the cause today means reading kernel trace dumps by hand.
3. Describe the system in TOML. Chronoscope simulates the Neutrino 8.0
   scheduler, audits every deadline, derives a verified fix, and reconciles
   against a real target.
4. Here is real output from the shipped brake-by-wire model.
   Non-deterministic, fifty-seven out of one hundred. Two missed deadlines,
   twelve priority inversions, and the blocking thread named.
5. The report is one offline HTML file with an interactive per-thread timeline,
   deadline markers, inversion shading and structured timing evidence.
6. The cause is a channel created with the fixed-priority flag, which disables
   QNX message-driven priority inheritance. A priority fifty client waits
   behind priority thirty logging.
7. The Determinism Autopilot does not guess. It generates eighteen valid QNX
   configurations, re-simulates and re-audits every one, and keeps only a
   measurable improvement.
8. One patch clears fixed priority on the brake channel. Score fifty-seven to
   one hundred, two deadline misses to zero, twelve inversions to zero.
   Counterfactually verified.
9. The repository also ships QNX 8.0 target code: native message passing, a
   resource manager, slogger2 instrumentation, and an instrumented mkifs image.
10. Real QNX artifacts close the loop. Import traceprinter, slog2info and pidin
    output, then reconcile the simulated model against measured target traces.
11. Everything is reproducible: an eleven-step hands-on codelab, sixty-nine
    tests, continuous integration, and an MIT licence.
12. Chronoscope for QNX 8.0. Prove exactly when your code runs. Clone the
    repository and try the sixty-second quickstart.

---

## Rule checks

| Rule | Status |
| --- | --- |
| Less than 3 minutes | PASS — duration printed by `build_video.py` and verifiable with `ffprobe` |
| Publicly visible on YouTube | **ACTION REQUIRED** — upload with the title/description above |
| Shows the project functioning | PASS — live CLI output and real rendered reports |
| Functioning on the device for which it was built | PASS — the host tool runs on macOS (the eligible host-based path); the QNX 8.0 target layer is shown as source, buildfile and build commands |
| No third-party trademarks / copyrighted music | PASS — original slides, original narration, plain-text platform references only |
| All materials in English | PASS |
