# common.mk -- shared QNX SDP 8.0 compiler settings
#
# QNX 8.0 is 64-bit only. Supported target triplets:
#   gcc_ntox86_64      (QEMU / VMware / generic x86-64)
#   gcc_ntoaarch64le   (Raspberry Pi 5, ARM64le)
#
# qcc selects the cross-compiler with -V<target> (or -V<version>,<target>).
# Reference: QNX SDP 8.0 > Programmer's Guide > Compiling and Debugging.

CPU          ?= x86_64
GCC_VERSION  ?=

TARGET_TRIPLE = gcc_nto$(CPU)
ifneq ($(strip $(GCC_VERSION)),)
  CCV = -V$(GCC_VERSION),$(TARGET_TRIPLE)
else
  CCV = -V$(TARGET_TRIPLE)
endif

# qnxsdp-env.sh puts qcc on PATH; honour it, but allow an explicit override.
QCC  ?= $(if $(QNX_HOST),$(QNX_HOST)/usr/bin/qcc,qcc)
MKIFS ?= $(if $(QNX_HOST),$(QNX_HOST)/usr/bin/mkifs,mkifs)

ROOT_DIR  := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
INC_DIR    = $(ROOT_DIR)/include
LIB_DIR    = $(ROOT_DIR)/lib
DEMO_DIR   = $(ROOT_DIR)/demo
RESMGR_DIR = $(ROOT_DIR)/resmgr
IMAGE_DIR  = $(ROOT_DIR)/images

WARNINGS = -Wall -Wextra -Wformat -Wshadow
DEFINES  = -D_POSIX_C_SOURCE=200809L -DQNX_8_0
CFLAGS   = $(CCV) $(WARNINGS) $(DEFINES) -O2 -g -I$(INC_DIR)
LDFLAGS  = $(CCV)
LDLIBS   = -lslog2 -lpthread

# Sources that make up the static instrumentation library.
LIB_SRCS = $(LIB_DIR)/chronoscope.c
LIB_OBJS = $(LIB_SRCS:.c=.o)
LIB_OUT  = $(ROOT_DIR)/libchronoscope.a
# qnxsdp-env.sh puts the target `nto<CPU>-ar` on PATH; plain `ar` also works
# for archiving ELF objects, so keep this overridable and simple.
AR       ?= ar

%.o: %.c
	$(QCC) $(CFLAGS) -c $< -o $@
