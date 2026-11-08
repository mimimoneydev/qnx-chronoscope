/*
 * chronoscope_demo.c -- reproduce (and fix) QNX priority inversion on target
 *
 * This is the runnable QNX 8.0 counterpart of models/brake-by-wire.toml:
 *
 *   pedal_sampler   prio 50   periodic client, 2 ms, 1.2 ms hard deadline
 *   brake_server    prio 20   synchronous MsgReceive()/MsgReply() server
 *   diag_logger     prio 30   a long medium-priority diagnostic burst
 *
 * With `_NTO_CHF_FIXED_PRIORITY` on the channel the server cannot inherit
 * priority 50, so the medium-priority logger delays it and the client misses
 * its deadline (unbounded priority inversion). Run with --inherit to clear
 * the flag: QNX message-driven priority inheritance boosts the server at
 * MsgSend() time and every deadline is met.
 *
 * Build:
 *   qcc -Vgcc_ntox86_64 -I../include ../lib/chronoscope.c \
 *       chronoscope_demo.c -o chronoscope_demo -lslog2
 * Run on QNX 8.0:
 *   slogger2 -d &
 *   ./chronoscope_demo --fixed-priority --iterations 10
 *   ./chronoscope_demo --inherit       --iterations 10
 *   slog2info -b chronoscope > /tmp/chrono.slog
 *   # copy /tmp/chrono.slog to the host, then:
 *   python3 -m chronoscope import /tmp/chrono.slog --format slog2 --audit
 *
 * QNX 8.0 APIs used: ChannelCreate(_NTO_CHF_FIXED_PRIORITY), ConnectAttach(),
 * MsgSend()/MsgReceive()/MsgReply(), timer_create() + SIGEV_PULSE_INIT,
 * ClockTime(), pthread_setschedparam(SCHED_FIFO), slogger2.
 */
#include "chronoscope.h"

#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <sys/neutrino.h>
#include <sys/netmgr.h>

#define PULSE_CODE_TICK   (_PULSE_CODE_MINAVAIL + 3)

typedef struct brake_msg {
    uint32_t  seq;
    uint32_t  service_us;
    uint64_t  sent_us;
} brake_msg_t;

typedef struct brake_reply {
    uint64_t  served_us;
    int       server_priority;
} brake_reply_t;

typedef struct demo_ctx {
    chrono_config_t cfg;
    int   server_chid;
    int   timer_chid;
    int   running;
} demo_ctx_t;

static demo_ctx_t g_demo;

/* ClockTime()-based busy work: deterministic CPU time, no sleep() jitter. */
static void busy_us(uint64_t us)
{
    uint64_t start = chrono_now_us();
    while (chrono_now_us() - start < us) {
        /* spin: models a WCET-sized compute step */
    }
}

static void *brake_server_main(void *unused)
{
    brake_msg_t   msg;
    brake_reply_t reply;
    int           rcvid;

    (void)unused;
    (void)chrono_thread_register("brake_server",
                                 (int)g_demo.cfg.server_priority);
    if (chrono_set_fifo_priority((int)g_demo.cfg.server_priority) != 0) {
        chrono_event("ERROR", "brake_server: pthread_setschedparam failed");
    }

    while (g_demo.running) {
        rcvid = MsgReceive(g_demo.server_chid, &msg, sizeof(msg), NULL);
        if (rcvid == -1) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        if (rcvid == 0) {
            continue;   /* pulse: no reply required in QNX */
        }
        chrono_event("MSG_RECEIVE", "channel=brake seq=%u", msg.seq);
        busy_us(msg.service_us);
        reply.served_us       = chrono_now_us();
        reply.server_priority = (int)g_demo.cfg.server_priority;
        if (MsgReply(rcvid, EOK, &reply, sizeof(reply)) == -1) {
            chrono_event("ERROR", "MsgReply failed: %s", strerror(errno));
        }
        chrono_msg_reply("brake", reply.server_priority);
    }
    return NULL;
}

static void *diag_logger_main(void *unused)
{
    (void)unused;
    (void)chrono_thread_register("diag_logger",
                                 (int)g_demo.cfg.logger_priority);
    if (chrono_set_fifo_priority((int)g_demo.cfg.logger_priority) != 0) {
        chrono_event("ERROR", "diag_logger: pthread_setschedparam failed");
    }
    chrono_event("DIAG_BURST", "compute=%u us", g_demo.cfg.logger_burst_us);
    busy_us(g_demo.cfg.logger_burst_us);
    return NULL;
}

static void usage(const char *prog)
{
    (void)fprintf(stderr,
        "usage: %s [--fixed-priority|--inherit] [--iterations N] "
        "[--period US] [--deadline US] [--service US]\n"
        "  --fixed-priority  ChannelCreate(_NTO_CHF_FIXED_PRIORITY): "
        "reproduces unbounded priority inversion (default)\n"
        "  --inherit         ChannelCreate(0): QNX message-driven priority "
        "inheritance fixes the deadlines\n", prog);
}

static uint32_t parse_u32(const char *s)
{
    unsigned long v = strtoul(s, NULL, 10);
    return (uint32_t)(v > 0xFFFFFFFFUL ? 0xFFFFFFFFUL : v);
}

int main(int argc, char **argv)
{
    pthread_t       server_tid, logger_tid;
    struct sigevent sev;
    struct itimerspec itime;
    timer_t         timerid;
    struct _pulse   pulse;
    int             coid, timer_coid, rcvid, i;
    uint32_t        iterations;
    uint64_t        worst = 0, misses = 0;

    g_demo.cfg.period_us        = 2000;
    g_demo.cfg.deadline_us      = 1200;
    g_demo.cfg.iterations       = 10;
    g_demo.cfg.client_priority  = 50;
    g_demo.cfg.server_priority  = 20;
    g_demo.cfg.logger_priority  = 30;
    g_demo.cfg.logger_burst_us  = 1800;
    g_demo.cfg.fixed_priority   = 1;
    g_demo.running = 1;

    for (i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--inherit") == 0) {
            g_demo.cfg.fixed_priority = 0;
        } else if (strcmp(argv[i], "--fixed-priority") == 0) {
            g_demo.cfg.fixed_priority = 1;
        } else if (strcmp(argv[i], "--iterations") == 0 && i + 1 < argc) {
            g_demo.cfg.iterations = parse_u32(argv[++i]);
        } else if (strcmp(argv[i], "--period") == 0 && i + 1 < argc) {
            g_demo.cfg.period_us = parse_u32(argv[++i]);
        } else if (strcmp(argv[i], "--deadline") == 0 && i + 1 < argc) {
            g_demo.cfg.deadline_us = parse_u32(argv[++i]);
        } else if (strcmp(argv[i], "--service") == 0 && i + 1 < argc) {
            g_demo.cfg.logger_burst_us = parse_u32(argv[++i]);
        } else {
            usage(argv[0]);
            return EXIT_FAILURE;
        }
    }

    if (chrono_init("chronoscope_demo") != 0) {
        (void)fprintf(stderr, "chrono_init failed\n");
        return EXIT_FAILURE;
    }
    (void)chrono_thread_register("pedal_sampler",
                                 (int)g_demo.cfg.client_priority);
    chrono_event("DEMO_CONFIG",
                 "fixed=%u period=%u deadline=%u client=%u server=%u logger=%u",
                 g_demo.cfg.fixed_priority, g_demo.cfg.period_us,
                 g_demo.cfg.deadline_us, g_demo.cfg.client_priority,
                 g_demo.cfg.server_priority, g_demo.cfg.logger_priority);

    /* QNX native message passing: ChannelCreate() with or without the flag
     * that disables message-driven priority inheritance. */
    g_demo.server_chid = ChannelCreate(g_demo.cfg.fixed_priority
                                       ? _NTO_CHF_FIXED_PRIORITY : 0);
    if (g_demo.server_chid == -1) {
        (void)fprintf(stderr, "ChannelCreate: %s\n", strerror(errno));
        return EXIT_FAILURE;
    }
    coid = ConnectAttach(ND_LOCAL_NODE, 0, g_demo.server_chid,
                         _NTO_SIDE_CHANNEL, 0);
    if (coid == -1) {
        (void)fprintf(stderr, "ConnectAttach: %s\n", strerror(errno));
        return EXIT_FAILURE;
    }

    /* A pulse-driven periodic timer (InterruptWait/SIGEV_INTR is the other
     * QNX option). SIGEV_PULSE_INIT keeps the release point in kernel space. */
    g_demo.timer_chid = ChannelCreate(0);
    timer_coid = ConnectAttach(ND_LOCAL_NODE, 0, g_demo.timer_chid,
                               _NTO_SIDE_CHANNEL, 0);
    SIGEV_PULSE_INIT(&sev, timer_coid, SIGEV_PULSE_PRIO_INHERIT,
                     PULSE_CODE_TICK, 0);
    if (timer_create(CLOCK_MONOTONIC, &sev, &timerid) == -1) {
        (void)fprintf(stderr, "timer_create: %s\n", strerror(errno));
        return EXIT_FAILURE;
    }
    memset(&itime, 0, sizeof(itime));
    itime.it_value.tv_nsec    = (long)g_demo.cfg.period_us * 1000L;
    itime.it_interval.tv_nsec = itime.it_value.tv_nsec;
    (void)timer_settime(timerid, 0, &itime, NULL);

    if (chrono_set_fifo_priority((int)g_demo.cfg.client_priority) != 0) {
        (void)fprintf(stderr, "warning: cannot set SCHED_FIFO priority "
                      "(run as root or with PROCMGR_AID_PRIORITY)\n");
    }
    (void)pthread_create(&server_tid, NULL, brake_server_main, NULL);
    (void)pthread_create(&logger_tid, NULL, diag_logger_main, NULL);

    iterations = g_demo.cfg.iterations ? g_demo.cfg.iterations : 10;
    for (i = 0; i < (int)iterations; ++i) {
        brake_msg_t   msg;
        brake_reply_t reply;
        uint64_t      release, response;

        /* Wait for the next timer pulse (QNX pulses arrive as rcvid == 0). */
        do {
            rcvid = MsgReceive(g_demo.timer_chid, &pulse, sizeof(pulse), NULL);
        } while (rcvid != 0 || pulse.code != PULSE_CODE_TICK);

        release = chrono_now_us();
        chrono_job_begin("pedal_cycle", (unsigned)i,
                         release + g_demo.cfg.deadline_us);
        busy_us(200);                       /* sensor acquisition */

        memset(&msg, 0, sizeof(msg));
        msg.seq        = (uint32_t)i;
        msg.service_us = 400;
        msg.sent_us    = chrono_now_us();
        chrono_msg_send("brake", (int)g_demo.cfg.client_priority);
        if (!g_demo.cfg.fixed_priority) {
            /* At MsgSend() time QNX boosts the server to the client's
             * priority; it does NOT revert on MsgReply(). */
            chrono_priority_boost("brake_server",
                                  (int)g_demo.cfg.server_priority,
                                  (int)g_demo.cfg.client_priority);
        }
        if (MsgSend(coid, &msg, sizeof(msg), &reply, sizeof(reply)) == -1) {
            chrono_event("ERROR", "MsgSend failed: %s", strerror(errno));
            break;
        }
        busy_us(100);                       /* actuator command */
        chrono_job_complete("pedal_cycle", (unsigned)i);

        response = chrono_now_us() - release;
        if (response > worst) {
            worst = response;
        }
        if (response > g_demo.cfg.deadline_us) {
            ++misses;
        }
    }

    g_demo.running = 0;
    (void)timer_delete(timerid);
    (void)pthread_join(logger_tid, NULL);
    /* ChannelDestroy() wakes the server's MsgReceive(); join it last. */
    (void)ChannelDestroy(g_demo.server_chid);
    (void)pthread_join(server_tid, NULL);
    (void)ConnectDetach(coid);
    (void)ChannelDestroy(g_demo.timer_chid);

    (void)printf("Chronoscope QNX 8.0 demo: fixed_priority=%u, "
                 "iterations=%u, misses=%llu, worst=%llu us\n",
                 g_demo.cfg.fixed_priority, iterations,
                 (unsigned long long)misses, (unsigned long long)worst);
    chrono_event("DEMO_SUMMARY", "misses=%llu worst_response=%llu",
                 (unsigned long long)misses, (unsigned long long)worst);
    chrono_shutdown();
    return EXIT_SUCCESS;
}
