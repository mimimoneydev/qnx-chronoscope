/*
 * chronoscope.c -- QNX 8.0 side of the Chronoscope determinism workbench
 *
 * Builds with:
 *   qcc -Vgcc_ntox86_64 -I../include -c chronoscope.c -o chronoscope.o
 *
 * The probe is deliberately tiny: one monotonic clock read, one formatted
 * slog2f() call per event, and a single thread-local job slot. Anything that
 * could perturb the timing of the loop being measured (malloc, mutexes,
 * file I/O, blocking writes) is kept out of the hot path. slogger2 buffers
 * asynchronously; `slog2info -w` on the target or `slog2info` over a serial
 * session extracts the CHRONO lines for the host-side importer.
 */
#include "chronoscope.h"

#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include <sys/neutrino.h>
#include <sys/slog2.h>
#include <sys/procmgr.h>
#ifdef CHRONO_ENABLE_KERNEL_TRACE
#include <sys/trace.h>
#endif

#define CHRONO_TLS_INITIALISED  0x43584F31u /* "CXO1" */

typedef struct chrono_job_slot {
    uint32_t     magic;
    char         name[CHRONO_MAX_NAME + 1];
    char         thread[CHRONO_MAX_NAME + 1];
    unsigned     index;
    uint64_t     start_us;
    uint64_t     deadline_us;
    int          priority;
} chrono_job_slot_t;

static pthread_mutex_t  g_lock = PTHREAD_MUTEX_INITIALIZER;
static slog2_buffer_t   g_buffer;
static int              g_slog2_ready = 0;
static int              g_initialised = 0;

/* One active job per thread. `static __thread` is supported by qcc/GCC. */
static __thread chrono_job_slot_t t_job;

/* --------------------------------------------------------------------- */
/* monotonic clock                                                       */
/* --------------------------------------------------------------------- */
uint64_t chrono_now_us(void)
{
    uint64_t ns = 0;
    /* QNX Neutrino: ClockTime(id, const uint64_t *new, uint64_t *old).
     * Passing NULL for `new` performs a pure read; old receives nanoseconds. */
    if (ClockTime(CLOCK_MONOTONIC, NULL, &ns) == -1) {
        struct timespec ts;
        if (clock_gettime(CLOCK_MONOTONIC, &ts) == -1) {
            return 0;
        }
        ns = (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
    }
    return ns / 1000ULL;
}

/* --------------------------------------------------------------------- */
/* slogger2 output                                                       */
/* --------------------------------------------------------------------- */
static void chrono_emit(const char *thread, const char *kind,
                        const char *fmt, va_list ap)
{
    char body[512];
    char line[640];
    uint64_t now = chrono_now_us();

    (void)vsnprintf(body, sizeof(body), fmt, ap);
    /* Wire format (see chronoscope.h and importers.py:parse_slog2info):
     *   CHRONO <monotonic_us> <thread> <KIND> <detail>
     * The timestamp is an INTEGER number of CLOCK_MONOTONIC microseconds,
     * not seconds.milliseconds -- the host reads it directly as t_us. */
    (void)snprintf(line, sizeof(line), "CHRONO %llu %s %s %s",
                   (unsigned long long)now, thread, kind, body);

    if (g_slog2_ready) {
        /* Non-blocking, severity INFO. slogger2 owns the buffering. */
        (void)slog2f(g_buffer, 0, SLOG2_INFO, "%s", line);
    } else {
        /* Graceful degradation: still usable over a serial console. */
        (void)fprintf(stderr, "%s\n", line);
    }
}

int chrono_init(const char *process_name)
{
    slog2_buffer_set_config_t cfg;
    int rc;

    memset(&cfg, 0, sizeof(cfg));
    cfg.buffer_set_name = CHRONO_SLOG2_SET_NAME;
    cfg.num_buffers     = 1;
    cfg.verbosity_level = SLOG2_INFO;
    cfg.buffer_config[0].buffer_name = CHRONO_SLOG2_BUF_NAME;
    cfg.buffer_config[0].num_pages   = CHRONO_SLOG2_PAGES;

    pthread_mutex_lock(&g_lock);
    if (!g_initialised) {
        rc = slog2_register(&cfg, &g_buffer, 0);
        if (rc == -1) {
            /* slogger2 not running yet: fall back to stderr, do not fail the
             * real-time application because logging is unavailable. */
            g_slog2_ready = 0;
        } else {
            g_slog2_ready = 1;
        }
        g_initialised = 1;
    }
    pthread_mutex_unlock(&g_lock);

    if (process_name != NULL) {
        chrono_event("PROBE_INIT", "name=%s qnx=8.0 slog2=%s",
                     process_name, g_slog2_ready ? "yes" : "stderr-fallback");
    }
    return 0;
}

void chrono_shutdown(void)
{
    pthread_mutex_lock(&g_lock);
    if (g_initialised) {
        chrono_event("PROBE_SHUTDOWN", "events emitted");
        /* slogger2 owns the buffer set and reclaims it when the process
         * exits; there is no unregister step required (or desirable in a
         * real-time process that may still be emitting from other threads). */
        g_slog2_ready = 0;
        g_initialised = 0;
    }
    pthread_mutex_unlock(&g_lock);
}

int chrono_thread_register(const char *name, int priority)
{
    if (name == NULL) {
        errno = EINVAL;
        return -1;
    }
    if (t_job.magic != CHRONO_TLS_INITIALISED) {
        memset(&t_job, 0, sizeof(t_job));
        t_job.magic = CHRONO_TLS_INITIALISED;
        t_job.priority = 1;
    }
    (void)snprintf(t_job.thread, sizeof(t_job.thread), "%s", name);
    t_job.priority = priority;
    chrono_event("THREAD_REGISTER", "%s prio=%d", name, priority);
    return 0;
}

static const char *chrono_thread_name(void)
{
    if (t_job.magic == CHRONO_TLS_INITIALISED && t_job.thread[0] != '\0') {
        return t_job.thread;
    }
    return "unnamed";
}

void chrono_event(const char *kind, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    chrono_emit(chrono_thread_name(), kind, fmt, ap);
    va_end(ap);
}

void chrono_job_begin(const char *job, unsigned index,
                      uint64_t absolute_deadline_us)
{
    if (t_job.magic != CHRONO_TLS_INITIALISED) {
        memset(&t_job, 0, sizeof(t_job));
        t_job.magic = CHRONO_TLS_INITIALISED;
    }
    (void)snprintf(t_job.name, sizeof(t_job.name), "%s", job ? job : "job");
    t_job.index       = index;
    t_job.start_us    = chrono_now_us();
    t_job.deadline_us = absolute_deadline_us;

    /* importers.py:parse_slog2info() parses `deadline=(\d+)` as an ABSOLUTE
     * CLOCK_MONOTONIC microsecond value. */
    chrono_event("JOB_RELEASE", "%s#%u deadline=%llu",
                 t_job.name, index, (unsigned long long)absolute_deadline_us);
}

void chrono_job_complete(const char *job, unsigned index)
{
    uint64_t end_us;
    uint64_t response_us;
    const char *status;

    (void)job;
    if (t_job.magic != CHRONO_TLS_INITIALISED || t_job.start_us == 0) {
        return;
    }
    end_us      = chrono_now_us();
    response_us = end_us >= t_job.start_us ? end_us - t_job.start_us : 0;
    status      = (t_job.deadline_us && end_us > t_job.deadline_us) ? "MISS" : "OK";

    chrono_event("JOB_COMPLETE", "%s#%u status=%s response=%llu",
                 t_job.name, index, status, (unsigned long long)response_us);
    t_job.start_us    = 0;
    t_job.deadline_us = 0;
}

void chrono_msg_send(const char *channel, int client_priority)
{
    chrono_event("MSG_SEND", "channel=%s client_prio=%d",
                 channel ? channel : "?", client_priority);
}

void chrono_msg_reply(const char *channel, int server_priority)
{
    chrono_event("MSG_REPLY", "channel=%s server_prio=%d",
                 channel ? channel : "?", server_priority);
}

void chrono_priority_boost(const char *thread, int from_priority, int to_priority)
{
    chrono_event("PRIORITY_BOOST", "%s %d -> %d",
                 thread ? thread : "?", from_priority, to_priority);
}

int chrono_grant_priority_ability(void)
{
    procmgr_ability_t abilities[2];
    unsigned flags;

    /* QNX 8.0: without PROCMGR_AID_PRIORITY a server that inherits priority
     * is clamped to (procnto -P ceiling - 1), i.e. 62 with the default -P 63.
     * See System Architecture > Priority inheritance and messages. */
    memset(abilities, 0, sizeof(abilities));
    abilities[0].ability = PROCMGR_AID_PRIORITY;
    abilities[0].flags   = PROCMGR_ADV_NONROOT | PROCMGR_AOP_ALLOW |
                           PROCMGR_AINHERIT_YES;
    abilities[1].ability = PROCMGR_AID_EOL;

    flags = 0;
    /* pid 0 == calling process (QNX C Library Reference > procmgr_ability). */
    if (procmgr_ability((pid_t)0, abilities, flags) == -1) {
        return -1;
    }
    chrono_event("ABILITY", "PROCMGR_AID_PRIORITY granted");
    return 0;
}

int chrono_set_fifo_priority(int priority)
{
    struct sched_param sp;
    int policy;

    memset(&sp, 0, sizeof(sp));
    sp.sched_priority = priority;
    /* QNX Neutrino supports SCHED_FIFO / SCHED_RR / SCHED_SPORADIC. */
    policy = SCHED_FIFO;
    if (pthread_setschedparam(pthread_self(), policy, &sp) != 0) {
        return -1;
    }
    return 0;
}

#ifdef CHRONO_ENABLE_KERNEL_TRACE
void chrono_kernel_trace_marker(unsigned code, uint64_t t_us)
{
    /* tracelogger must be in daemon mode (`tracelogger -d1`) and the image
     * must boot procnto-smp-instr. User-string events appear in the same
     * `traceprinter` stream as THRUNNING / THSEND / THREPLY. */
    (void)TraceEvent(_NTO_TRACE_INSERTUSRSTREVENT, code,
                     (unsigned)(t_us & 0xFFFFFFFFULL),
                     (unsigned)(t_us >> 32));
}
#endif
