/*
 * chronoscope_resmgr.c -- /dev/chronoscope QNX 8.0 resource manager
 *
 * This is the target-side control plane for the Chronoscope probe. A QNX
 * resource manager is the idiomatic way to expose a device entry in the QNX
 * path namespace: resmgr_attach() registers /dev/chronoscope and devctl()
 * gives the host (or the demo process) a typed, synchronous command channel.
 *
 * Builds with:
 *   qcc -Vgcc_ntox86_64 -I../include ../lib/chronoscope.c \
 *       chronoscope_resmgr.c -o chronoscope_resmgr -lslog2
 *
 * QNX 8.0 APIs used here:
 *   dispatch_create()/dispatch_block()/dispatch_handler()  sys/dispatch.h
 *   resmgr_attach(), iofunc_attr_init(), iofunc_func_init() sys/iofunc.h
 *   io_devctl / _DEVCTL_DATA / _RESMGR_PTR                 devctl.h
 *   procmgr_ability() via chrono_grant_priority_ability()  sys/procmgr.h
 *   slog2f() via the probe library                          sys/slog2.h
 *
 * Run on the target:
 *   chronoscope_resmgr &
 *   devctl /dev/chronoscope start      # or via chrono_config_t + devctl()
 */
#include "chronoscope.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <devctl.h>
#include <sys/dispatch.h>
#include <sys/iofunc.h>
#include <sys/neutrino.h>

static chrono_config_t g_config = {
    .period_us        = 2000,
    .deadline_us      = 1200,
    .iterations       = 10,
    .client_priority  = 50,
    .server_priority  = 20,
    .logger_priority  = 30,
    .logger_burst_us  = 1800,
    .fixed_priority   = 1,       /* reproduce the seeded defect by default */
};

static chrono_stats_t g_stats;
static pthread_mutex_t g_state_lock = PTHREAD_MUTEX_INITIALIZER;

/* --------------------------------------------------------------------- */
/* devctl handler                                                        */
/* --------------------------------------------------------------------- */
static int chrono_io_devctl(resmgr_context_t *ctp, io_devctl_t *msg,
                            iofunc_ocb_t *ocb)
{
    int      status;
    int      nbytes = 0;
    void    *data;

    /* Let iofunc handle the standard devctls (e.g. _DCMD_ALL_GETFLAGS). */
    status = iofunc_devctl_default(ctp, msg, ocb);
    if (status != _RESMGR_DEFAULT) {
        return status;
    }

    /* QNX keeps the devctl payload immediately after the io_devctl_t header. */
    data = _DEVCTL_DATA(msg->i);
    status = EOK;

    pthread_mutex_lock(&g_state_lock);
    switch (msg->i.dcmd) {
    case CHRONO_DCMD_START: {
        chrono_config_t *cfg = (chrono_config_t *)data;
        if (cfg->period_us == 0 || cfg->deadline_us == 0) {
            status = EINVAL;
            break;
        }
        g_config = *cfg;
        g_stats.running = 1;
        chrono_event("RESMGR_START",
                     "period=%u deadline=%u client=%u server=%u logger=%u fixed=%u",
                     g_config.period_us, g_config.deadline_us,
                     g_config.client_priority, g_config.server_priority,
                     g_config.logger_priority, g_config.fixed_priority);
        break;
    }
    case CHRONO_DCMD_STOP:
        g_stats.running = 0;
        chrono_event("RESMGR_STOP", "jobs=%llu misses=%llu",
                     (unsigned long long)g_stats.jobs,
                     (unsigned long long)g_stats.misses);
        break;

    case CHRONO_DCMD_GETSTATS: {
        chrono_stats_t *out = (chrono_stats_t *)data;
        g_stats.fixed_priority = g_config.fixed_priority;
        *out = g_stats;
        nbytes = (int)sizeof(*out);
        break;
    }
    case CHRONO_DCMD_SETFIXED: {
        uint32_t *flag = (uint32_t *)data;
        g_config.fixed_priority = (*flag) ? 1u : 0u;
        g_stats.fixed_priority = g_config.fixed_priority;
        /* This is the QNX-specific switch the host autopilot derives:
         * clearing _NTO_CHF_FIXED_PRIORITY re-enables message-driven
         * priority inheritance at MsgSend() time. */
        chrono_event("CHANNEL_FLAG", "_NTO_CHF_FIXED_PRIORITY=%u",
                     g_config.fixed_priority);
        break;
    }
    case CHRONO_DCMD_DUMP:
        chrono_event("RESMGR_DUMP",
                     "jobs=%llu misses=%llu worst_response=%llu boosts=%llu fixed=%u",
                     (unsigned long long)g_stats.jobs,
                     (unsigned long long)g_stats.misses,
                     (unsigned long long)g_stats.worst_response_us,
                     (unsigned long long)g_stats.priority_boosts,
                     g_config.fixed_priority);
        break;
    default:
        status = ENOSYS;
        break;
    }
    pthread_mutex_unlock(&g_state_lock);

    memset(&msg->o, 0, sizeof(msg->o));
    msg->o.ret_val = status;
    if (nbytes > 0) {
        /* Reply with the header plus the outgoing payload. */
        return _RESMGR_PTR(ctp, &msg->o, sizeof(msg->o) + (size_t)nbytes);
    }
    return EOK;
}

/* --------------------------------------------------------------------- */
/* process entry                                                         */
/* --------------------------------------------------------------------- */
int main(int argc, char **argv)
{
    dispatch_t          *dpp;
    dispatch_context_t  *ctp;
    resmgr_attr_t        rattr;
    resmgr_connect_funcs_t cfuncs;
    resmgr_io_funcs_t     ifuncs;
    iofunc_attr_t         attr;
    int                   id;

    (void)argc;
    (void)argv;

    if (chrono_init("chronoscope_resmgr") != 0) {
        (void)fprintf(stderr, "chronoscope_resmgr: chrono_init failed\n");
        return EXIT_FAILURE;
    }
    (void)chrono_thread_register("resmgr_main", 20);

    /* QNX 8.0: a server without PROCMGR_AID_PRIORITY is clamped to the
     * highest unprivileged priority when it inherits a client's priority.
     * Request the ability explicitly; on a locked-down image this may be
     * denied, which is reported but does not prevent the device from working. */
    if (chrono_grant_priority_ability() != 0) {
        chrono_event("ABILITY_DENIED",
                     "PROCMGR_AID_PRIORITY unavailable; use a secpol policy");
    }

    dpp = dispatch_create();
    if (dpp == NULL) {
        (void)fprintf(stderr, "chronoscope_resmgr: dispatch_create: %s\n",
                      strerror(errno));
        return EXIT_FAILURE;
    }

    memset(&rattr, 0, sizeof(rattr));
    rattr.nparts_max   = 1;
    rattr.msg_max_size = 4096;

    iofunc_func_init(_RESMGR_CONNECT_NFUNCS, &cfuncs,
                     _RESMGR_IO_NFUNCS, &ifuncs);
    ifuncs.devctl = chrono_io_devctl;

    iofunc_attr_init(&attr, S_IFCHR | 0666, NULL, NULL);
    attr.mount = NULL;

    id = resmgr_attach(dpp, &rattr, CHRONO_DEVICE_PATH, _FTYPE_ANY, 0,
                       &cfuncs, &ifuncs, &attr);
    if (id == -1) {
        (void)fprintf(stderr, "chronoscope_resmgr: resmgr_attach: %s\n",
                      strerror(errno));
        return EXIT_FAILURE;
    }

    chrono_event("RESMGR_READY", "path=%s", CHRONO_DEVICE_PATH);

    ctp = dispatch_context_alloc(dpp);
    if (ctp == NULL) {
        (void)fprintf(stderr, "chronoscope_resmgr: no context\n");
        return EXIT_FAILURE;
    }
    for (;;) {
        ctp = dispatch_block(ctp);
        if (ctp == NULL) {
            break;
        }
        dispatch_handler(ctp);
    }

    chrono_shutdown();
    return EXIT_SUCCESS;
}
