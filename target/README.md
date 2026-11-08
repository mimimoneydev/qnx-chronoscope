# Chronoscope · QNX 8.0 target-side probe

This directory contains the code that runs **on QNX Neutrino 8.0**. The host
side (`chronoscope/*.py`, `models/*.toml`) is a dependency-free Python
workbench that models, audits and repairs QNX timing behaviour; this directory
is what makes the connection to a real QNX target concrete: a native C probe,
a resource manager, an `mkifs` image, and scripts that capture real
`traceprinter` / `slog2info` / `pidin` artifacts for the host importers.

## Layout

| Path | Purpose |
| --- | --- |
| `include/chronoscope.h` | Public probe API + `/dev/chronoscope` devctl commands |
| `lib/chronoscope.c` | `ClockTime()`, `slog2f()`, CHRONO job annotations, `procmgr_ability()` |
| `resmgr/chronoscope_resmgr.c` | QNX resource manager at `/dev/chronoscope` |
| `demo/chronoscope_demo.c` | Real-time demo: reproduces and fixes QNX priority inversion |
| `common.mk`, `Makefile` | `qcc` cross-build for `x86_64` and `aarch64le` |
| `images/chronoscope.build` | `mkifs` buildfile (instrumented kernel, slogger2, tracelogger) |
| `scripts/build-target.sh` | SDP environment + build + `mkifs` |
| `scripts/run-qemu.sh` | Boot the IFS under QEMU (x86_64) |
| `scripts/run-rpi5.md` | Raspberry Pi 5 / QNX Everywhere deployment |

## Exact build commands

QNX SDP 8.0 / QNX Everywhere is required (free for non-commercial use).
QNX 8.0 is 64-bit only.

```sh
source $HOME/qnx800/qnxsdp-env.sh          # sets QNX_HOST, QNX_TARGET
cd target
./scripts/build-target.sh x86_64           # QEMU / VMware
# or
./scripts/build-target.sh aarch64le        # Raspberry Pi 5
```

Equivalent direct commands:

```sh
make CPU=x86_64 all                        # qcc -Vgcc_ntox86_64
make CPU=aarch64le GCC_VERSION=12.0 all    # qcc -V12.0,gcc_ntoaarch64le
make CPU=x86_64 image                      # mkifs -> images/chronoscope-x86_64.ifs
```

Lint the buildfile from the host (no QNX install needed):

```sh
python3 -m chronoscope inspect target/images/chronoscope.build \
    --model models/brake-by-wire.toml
```

## Exact run commands on the QNX 8.0 target

```sh
slogger2 -d &
tracelogger -d1 -f /tmp/chronoscope.kev &
/usr/bin/chronoscope_resmgr &
/usr/bin/chronoscope_demo --fixed-priority --iterations 10   # defective
/usr/bin/chronoscope_demo --inherit       --iterations 10   # fixed
```

Capture, then hand the artifacts to the host workbench:

```sh
slog2info -b chronoscope > /tmp/chronoscope.slog
traceprinter -f /tmp/chronoscope.kev > /tmp/qnx-kernel-trace.txt
pidin -F '%P %T %n %p %S' > /tmp/pidin.txt
```

```sh
python3 -m chronoscope import /tmp/qnx-kernel-trace.txt --audit
python3 -m chronoscope import /tmp/chronoscope.slog --format slog2 --audit
python3 -m chronoscope reconcile demo/brake-by-wire.json \
    /tmp/qnx-kernel-trace.txt
```

## QNX 8.0 API → file → function (Evidence of QNX use)

| QNX 8.0 API / concept | File | Function / location | Why it matters |
| --- | --- | --- | --- |
| `ClockTime(CLOCK_MONOTONIC)` | `lib/chronoscope.c` | `chrono_now_us()` | monotonic microsecond timeline, same clock as target measurements |
| `slog2f()` / `slogger2` | `lib/chronoscope.c` | `chrono_emit()` | structured, low-overhead event capture |
| `TraceEvent(_NTO_TRACE_INSERTUSRSTREVENT)` | `lib/chronoscope.c` | `chrono_kernel_trace_marker()` (optional, `-DCHRONO_ENABLE_KERNEL_TRACE`) | probe markers inside the instrumented kernel trace |
| `procmgr_ability(PROCMGR_AID_PRIORITY)` | `lib/chronoscope.c` | `chrono_grant_priority_ability()` | lets a boosted server exceed the unprivileged priority ceiling |
| `pthread_setschedparam(SCHED_FIFO)` | `lib/chronoscope.c` | `chrono_set_fifo_priority()` | QNX 256-level fixed-priority scheduling |
| `ChannelCreate(_NTO_CHF_FIXED_PRIORITY)` | `demo/chronoscope_demo.c` | `main()` | the seeded defect that disables message-driven priority inheritance |
| `ConnectAttach()` | `demo/chronoscope_demo.c` | `main()` | connection for synchronous message passing |
| `MsgSend()` / `MsgReceive()` / `MsgReply()` | `demo/chronoscope_demo.c` | client loop / `brake_server_main()` | native QNX synchronous IPC, `STATE_SEND` / `STATE_RECEIVE` / `STATE_REPLY` |
| `timer_create()` + `SIGEV_PULSE_INIT` | `demo/chronoscope_demo.c` | `main()` | pulse-driven periodic release, no signal-handler jitter |
| `dispatch_create()` / `resmgr_attach()` | `resmgr/chronoscope_resmgr.c` | `main()` | QNX path-space resource manager at `/dev/chronoscope` |
| `iofunc_*` / `io_devctl()` / `_DEVCTL_DATA` | `resmgr/chronoscope_resmgr.c` | `chrono_io_devctl()` | typed host↔target control channel |
| `procnto-smp-instr -P 64` | `images/chronoscope.build` | bootstrap | instrumented microkernel + explicit priority ceiling |
| `tracelogger`, `traceprinter` | `images/chronoscope.build` | startup script | capture TH\* kernel events the host importer parses |
| `mkifs`, `qcc -Vgcc_ntox86_64` | `Makefile`, `common.mk`, `scripts/` | build | QNX 8.0 image / toolchain |

## Verified vs. requires QNX hardware

| Claim | Status |
| --- | --- |
| Host Python tool (simulate / audit / fix / import / report / verify) | Verified on macOS with Python 3.13; `python3 -m chronoscope verify` |
| Buildfile determinism checks | Verified by `chronoscope inspect` (host-side, no SDP needed) |
| C sources compile with `qcc` | **Requires QNX SDP 8.0** — not compiled in the development environment |
| Demo executes on a QNX 8.0 target | **Requires QNX Everywhere VM / Raspberry Pi 5** |
| Real `traceprinter` capture imported and reconciled | Requires a target run; the importer is verified against a `traceprinter`-compatible export |

This distinction is stated deliberately: the submission does not claim
on-target execution unless the captured artifacts are actually included.
