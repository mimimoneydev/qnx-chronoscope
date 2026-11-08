/*
 * chronoscope.h -- QNX 8.0 target-side instrumentation probe
 *
 * Chronoscope for QNX 8.0 : "prove exactly when your code runs".
 *
 * The host-side workbench (chronoscope/*.py) audits a QNX Neutrino 8.0
 * timing model and imports real target artifacts. This header is the public
 * API of the small C probe that runs ON the QNX target and produces those
 * artifacts. It uses only QNX SDP 8.0 / POSIX APIs.
 *
 * QNX 8.0 references:
 *   - ClockTime()                      C Library Reference / sys/neutrino.h
 *   - slog2f(), slogger2              System Analysis Toolkit / sys/slog2.h
 *   - procmgr_ability()               C Library Reference
 *   - _NTO_CHF_FIXED_PRIORITY         C Library Reference > ChannelCreate()
 *   - devctl(), __DIO* macros         C Library Reference > devctl()
 *
 * The CHRONO wire format consumed by chronoscope/importers.py is:
 *
 *   CHRONO <monotonic_us> <thread> JOB_RELEASE   <job>#<n> deadline=<abs_us>
 *   CHRONO <monotonic_us> <thread> JOB_COMPLETE  <job>#<n> status=OK response=<us>
 *
 * `<monotonic_us>` is an unsigned INTEGER count of CLOCK_MONOTONIC
 * microseconds (not seconds.milliseconds): the host importer reads it
 * directly as t_us. `deadline=` is an ABSOLUTE CLOCK_MONOTONIC microsecond
 * value, not a relative deadline. This matches Chronoscope's
 * traceprinter-compatible export (chronoscope/trace.py:to_traceprinter_text())
 * and chronoscope/importers.py:parse_slog2info().
 */
#ifndef CHRONOSCOPE_H
#define CHRONOSCOPE_H

#include <stdarg.h>
#include <stdint.h>
#include <devctl.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CHRONO_DEVICE_PATH      "/dev/chronoscope"
#define CHRONO_RES_NAME         "chronoscope"
#define CHRONO_MAX_NAME         63
#define CHRONO_SLOG2_SET_NAME   "chronoscope"
#define CHRONO_SLOG2_BUF_NAME   "events"
#define CHRONO_SLOG2_PAGES      8

/* --------------------------------------------------------------------- */
/* devctl() interface of /dev/chronoscope                                */
/* --------------------------------------------------------------------- */
/* QNX encodes direction/size in the command word. __DIOT = host->resource
 * manager, __DIOF = resource manager->host, __DION = no payload. */
typedef struct chrono_config {
    uint32_t period_us;          /* control-loop period                  */
    uint32_t deadline_us;        /* hard deadline, relative to release   */
    uint32_t iterations;         /* number of control cycles, 0 = until stopped */
    uint32_t client_priority;    /* SCHED_FIFO priority of high-prio client    */
    uint32_t server_priority;    /* SCHED_FIFO priority of message server      */
    uint32_t logger_priority;    /* SCHED_FIFO priority of medium-prio noise   */
    uint32_t logger_burst_us;    /* length of the medium-priority interference */
    uint32_t fixed_priority;     /* 1 = _NTO_CHF_FIXED_PRIORITY (the defect)  */
    uint32_t reserved[4];
} chrono_config_t;

typedef struct chrono_stats {
    uint64_t jobs;
    uint64_t misses;
    uint64_t worst_response_us;
    uint64_t last_response_us;
    uint64_t priority_boosts;
    uint32_t running;
    uint32_t fixed_priority;
    uint32_t reserved[6];
} chrono_stats_t;

#define CHRONO_DCMD_START       __DIOT(_DCMD_MISC, 1, chrono_config_t)
#define CHRONO_DCMD_STOP        __DION(_DCMD_MISC, 2)
#define CHRONO_DCMD_GETSTATS    __DIOF(_DCMD_MISC, 3, chrono_stats_t)
#define CHRONO_DCMD_SETFIXED    __DIOT(_DCMD_MISC, 4, uint32_t)
#define CHRONO_DCMD_DUMP        __DION(_DCMD_MISC, 5)

/* --------------------------------------------------------------------- */
/* Probe library                                                        */
/* --------------------------------------------------------------------- */

/* Register the slogger2 buffer set. Call once, before any other API.
 * Returns 0 on success, -1 on failure (errno set). */
int  chrono_init(const char *process_name);
void chrono_shutdown(void);

/* Bind a human-readable name to the calling thread. The host importer uses
 * this name as the timeline lane. `priority` is the effective SCHED_FIFO
 * priority; it is emitted as `prio=<n>` so `chronoscope import` can rebuild
 * priority-inversion episodes from a real trace. */
int  chrono_thread_register(const char *name, int priority);

/* CLOCK_MONOTONIC in microseconds (QNX ClockTime()). Never wraps in practice
 * for a 64-bit counter at 1 ns resolution. */
uint64_t chrono_now_us(void);

/* Emit a job release with an ABSOLUTE deadline in monotonic microseconds. */
void chrono_job_begin(const char *job, unsigned index, uint64_t absolute_deadline_us);

/* Emit the matching job complete line, including the measured response. */
void chrono_job_complete(const char *job, unsigned index);

/* Free-form structured annotation: kind must be an uppercase token. */
void chrono_event(const char *kind, const char *fmt, ...);

/* Annotations for QNX native synchronous message passing. */
void chrono_msg_send(const char *channel, int client_priority);
void chrono_msg_reply(const char *channel, int server_priority);
void chrono_priority_boost(const char *thread, int from_priority, int to_priority);

/* Raise the process's priority ability, mirroring PROCMGR_AID_PRIORITY.
 * QNX 8.0 clamps message-driven inheritance to the highest unprivileged
 * priority unless the server holds this ability. Returns 0 on success. */
int  chrono_grant_priority_ability(void);

/* Set the calling thread's SCHED_FIFO priority. Returns 0 on success. */
int  chrono_set_fifo_priority(int priority);

#ifdef CHRONO_ENABLE_KERNEL_TRACE
/* Optional: insert a user event into the instrumented kernel trace so
 * `tracelogger` + `traceprinter` show probe markers alongside TH* events.
 * Requires procnto-smp-instr and a running tracelogger daemon. */
void chrono_kernel_trace_marker(unsigned code, uint64_t t_us);
#endif

#ifdef __cplusplus
}
#endif
#endif /* CHRONOSCOPE_H */
