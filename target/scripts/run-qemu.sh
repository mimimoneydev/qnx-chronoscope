#!/bin/sh
# Boot the Chronoscope QNX 8.0 IFS under QEMU (x86_64).
#
# QNX Everywhere ships a preconfigured QEMU/VMware QNX 8.0 target image and
# is the recommended way to run this project. This script is the lightweight
# alternative for a raw IFS produced by mkifs; adapt disk/network options to
# the QNX BSP you are using.
#
#   ./run-qemu.sh [path/to/chronoscope-x86_64.ifs]
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET_DIR=$(dirname -- "$SCRIPT_DIR")
IFS_IMAGE="${1:-$TARGET_DIR/images/chronoscope-x86_64.ifs}"
MEMORY="${QEMU_MEMORY:-1024}"
CPUS="${QEMU_CPUS:-2}"

if [ ! -f "$IFS_IMAGE" ]; then
    echo "error: image not found: $IFS_IMAGE" >&2
    echo "       run ./scripts/build-target.sh x86_64 first" >&2
    exit 2
fi

if command -v qemu-system-x86_64 >/dev/null 2>&1; then
    QEMU=qemu-system-x86_64
elif command -v qemu-system-aarch64 >/dev/null 2>&1; then
    echo "error: aarch64 QEMU found but this image is x86_64" >&2
    exit 2
else
    echo "error: qemu-system-x86_64 not found." >&2
    echo "       Install QEMU, or use the QNX Everywhere Developer Desktop VM." >&2
    exit 2
fi

echo "Booting $IFS_IMAGE with $CPUS CPU(s), ${MEMORY} MiB RAM"
echo "Serial console is attached to stdio; log in as root."
echo ""
echo "On the target, capture evidence with:"
echo "  slog2info -b chronoscope > /tmp/chronoscope.slog"
echo "  traceprinter -f /tmp/chronoscope.kev > /tmp/qnx-kernel-trace.txt"
echo "  pidin -F '%P %T %n %p %S' > /tmp/pidin.txt"

exec "$QEMU" \
    -m "$MEMORY" \
    -smp "$CPUS" \
    -drive "file=$IFS_IMAGE,format=raw,if=ide" \
    -serial stdio \
    -no-reboot \
    -display none
