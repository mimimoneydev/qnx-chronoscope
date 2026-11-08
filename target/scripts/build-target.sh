#!/bin/sh
# Build the Chronoscope QNX 8.0 target-side binaries and bootable image.
#
#   ./build-target.sh x86_64        # QEMU / VMware / generic x86-64
#   ./build-target.sh aarch64le     # Raspberry Pi 5 / ARM64le
#
# Requires QNX SDP 8.0 (QNX Everywhere for non-commercial use).
set -eu

CPU="${1:-x86_64}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET_DIR=$(dirname -- "$SCRIPT_DIR")

case "$CPU" in
    x86_64|aarch64le) ;;
    *) echo "error: CPU must be x86_64 or aarch64le (QNX 8.0 is 64-bit only)" >&2
       exit 2 ;;
esac

# Locate and source the QNX SDP environment unless it is already active.
if [ -z "${QNX_HOST:-}" ] || [ -z "${QNX_TARGET:-}" ]; then
    QNX_SDP_DIR="${QNX_SDP_DIR:-$HOME/qnx800}"
    for candidate in \
        "$QNX_SDP_DIR/qnxsdp-env.sh" \
        "$QNX_SDP_DIR/qnx_set_env.sh" \
        "/opt/qnx800/qnxsdp-env.sh"; do
        if [ -f "$candidate" ]; then
            echo "Sourcing QNX SDP 8.0 environment: $candidate"
            # shellcheck disable=SC1090
            . "$candidate"
            break
        fi
    done
fi

if [ -z "${QNX_HOST:-}" ] || [ -z "${QNX_TARGET:-}" ]; then
    echo "error: QNX_HOST/QNX_TARGET not set." >&2
    echo "       Install QNX SDP 8.0 / QNX Everywhere, then either source" >&2
    echo "       qnxsdp-env.sh or set QNX_SDP_DIR to its install directory." >&2
    exit 2
fi

command -v qcc   >/dev/null 2>&1 || { echo "error: qcc not on PATH" >&2; exit 2; }
command -v mkifs >/dev/null 2>&1 || { echo "error: mkifs not on PATH" >&2; exit 2; }

echo "QNX_HOST   = $QNX_HOST"
echo "QNX_TARGET = $QNX_TARGET"
echo "CPU        = $CPU"

cd "$TARGET_DIR"
make clean
make CPU="$CPU" all
make CPU="$CPU" image

echo ""
echo "Built:"
echo "  $TARGET_DIR/libchronoscope.a"
echo "  $TARGET_DIR/demo/chronoscope_demo"
echo "  $TARGET_DIR/resmgr/chronoscope_resmgr"
echo "  $TARGET_DIR/images/chronoscope-$CPU.ifs"
echo ""
echo "Next: ./scripts/run-qemu.sh   (x86_64) or follow scripts/run-rpi5.md"
