# Makefile -- Chronoscope QNX 8.0 target-side probe and demo
#
# Prerequisites: QNX SDP 8.0 / QNX Everywhere with qcc and mkifs on PATH.
#   source $HOME/qnx800/qnxsdp-env.sh
#   make CPU=x86_64
#   make CPU=aarch64le
#   make CPU=x86_64 image
#   make clean
#
# On an aarch64le build with a specific toolchain version:
#   make CPU=aarch64le GCC_VERSION=12.0
#
# NOTE: these binaries are cross-compiled for QNX Neutrino 8.0; they will not
# run on macOS/Linux/Windows. The host-side Python tool runs anywhere.

include common.mk

.PHONY: all lib demo resmgr image install clean help

all: lib demo resmgr

lib: $(LIB_OUT)

$(LIB_OUT): $(LIB_OBJS)
	$(AR) rcs $@ $^

$(DEMO_DIR)/chronoscope_demo: $(DEMO_DIR)/chronoscope_demo.c $(LIB_OUT) $(INC_DIR)/chronoscope.h
	$(QCC) $(CFLAGS) $< $(LIB_OUT) $(LDFLAGS) $(LDLIBS) -o $@

$(RESMGR_DIR)/chronoscope_resmgr: $(RESMGR_DIR)/chronoscope_resmgr.c $(LIB_OUT) $(INC_DIR)/chronoscope.h
	$(QCC) $(CFLAGS) $< $(LIB_OUT) $(LDFLAGS) $(LDLIBS) -o $@

demo: $(DEMO_DIR)/chronoscope_demo
resmgr: $(RESMGR_DIR)/chronoscope_resmgr

# mkifs consumes the .build buildfile; the resulting IFS is bootable on the
# chosen 64-bit target. See target/images/chronoscope.build.
image: lib demo resmgr
	$(MKIFS) $(IMAGE_DIR)/chronoscope.build $(IMAGE_DIR)/chronoscope-$(CPU).ifs

install: all
	@echo "Copy to the QNX target (adjust TARGET_IP / TARGET_USER):"
	@echo "  scp $(DEMO_DIR)/chronoscope_demo $(RESMGR_DIR)/chronoscope_resmgr \"
	@echo "      \$${TARGET_USER:-root}@\$${TARGET_IP}:/usr/bin/"
	@echo "Then on the target: chronoscope_resmgr & ; chronoscope_demo --inherit"

clean:
	$(RM) $(LIB_OBJS) $(LIB_OUT) \
	      $(DEMO_DIR)/chronoscope_demo $(RESMGR_DIR)/chronoscope_resmgr \
	      $(IMAGE_DIR)/chronoscope-*.ifs

help:
	@echo "Targets: all lib demo resmgr image install clean"
	@echo "CPU=x86_64|aarch64le (default x86_64), GCC_VERSION optional"
