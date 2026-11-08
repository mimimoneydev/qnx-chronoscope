# Raspberry Pi 5 · QNX 8.0 (aarch64le)

This guide deploys the Chronoscope QNX-side probe to a Raspberry Pi 5 running
the QNX Everywhere 8.0 quick-start image. QNX 8.0 is 64-bit only, so the
target triplet is `gcc_ntoaarch64le`.

## 1. Prerequisites

| Item | Notes |
| --- | --- |
| QNX SDP 8.0 / QNX Everywhere | free for non-commercial use; includes `qcc`, `mkifs` |
| Raspberry Pi 5 quick-start image | QNX Everywhere download, flashed to microSD |
| USB↔TTL serial cable | 3.3 V, for the console (`115200 8N1`) |
| Host with Python 3.11+ | for the Chronoscope host-side tool |

## 2. Build for aarch64le

```sh
export QNX_SDP_DIR="$HOME/qnx800"       # adjust to your install
cd target
./scripts/build-target.sh aarch64le
```

This produces `libchronoscope.a`, `demo/chronoscope_demo`,
`resmgr/chronoscope_resmgr` and `images/chronoscope-aarch64le.ifs`.
To select an explicit compiler version:

```sh
make CPU=aarch64le GCC_VERSION=12.0 all
# equivalent: qcc -V12.0,gcc_ntoaarch64le ...
```

> The `[virtual=x86_64,elf]` line in `images/chronoscope.build` must be changed
> to `[virtual=aarch64le,elf]` and the bootstrap line to `startup-rpi5` (or the
> startup binary shipped with your QNX BSP) for a Pi 5 image. The buildfile is
> written for the x86_64 QEMU target by default; the linter accepts either
> 64-bit architecture.

## 3. Deploy the binaries

Copy the two binaries to the running target (network or SD card):

```sh
scp demo/chronoscope_demo resmgr/chronoscope_resmgr root@<target-ip>:/usr/bin/
chmod +x /usr/bin/chronoscope_demo /usr/bin/chronoscope_resmgr
```

On the serial console:

```sh
slogger2 -d &
tracelogger -d1 -f /tmp/chronoscope.kev &
/usr/bin/chronoscope_resmgr &
```

## 4. Reproduce and fix priority inversion on real hardware

```sh
# Defective configuration: _NTO_CHF_FIXED_PRIORITY disables inheritance
/usr/bin/chronoscope_demo --fixed-priority --iterations 10

# QNX message-driven priority inheritance re-enabled
/usr/bin/chronoscope_demo --inherit --iterations 10
```

Both runs emit `CHRONO <t_us> <thread> JOB_RELEASE ...` lines into slogger2,
plus kernel TH\* events into the tracelogger buffer.

## 5. Capture the evidence

```sh
slog2info -b chronoscope > /tmp/chronoscope.slog
traceprinter -f /tmp/chronoscope.kev > /tmp/qnx-kernel-trace.txt
pidin -F '%P %T %n %p %S' > /tmp/pidin.txt
```

Copy these three files back to the host and feed them to the workbench:

```sh
python3 -m chronoscope import /tmp/chronoscope.slog --format slog2 --audit
python3 -m chronoscope import /tmp/qnx-kernel-trace.txt --audit
python3 -m chronoscope import /tmp/pidin.txt --format pidin
python3 -m chronoscope reconcile demo/brake-by-wire.json \
    /tmp/qnx-kernel-trace.txt --json reconcile.json
```

`reconcile` compares the simulated WCET per thread against the measured QNX
WCET and reports drift, which makes the model falsifiable rather than merely
illustrative.

## Honest status

| Step | Where verified |
| --- | --- |
| Host-side model, audit, autopilot, importers | macOS / Linux / Windows, `python3 -m chronoscope verify` |
| C sources compile-clean under QNX `qcc` | **requires QNX SDP 8.0** (not available in the development environment) |
| Demo runs on QNX 8.0 target / Pi 5 | **requires QNX Everywhere hardware or VM** |

The C sources are written against the QNX SDP 8.0 API and must be compiled
with `qcc` on a QNX host. No claim of on-target execution is made in the
submission unless these steps have been performed and their output included.
