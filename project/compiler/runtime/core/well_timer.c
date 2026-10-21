#include "wellrt.h"
#include "well_internal.h"

#include <stdlib.h>
#include <string.h>

/* Core timer service: a single timer thread wakes every ~10 ms (the portable
 * cond-wait granularity), collects due timers and dispatches each one as a
 * scheduler task, so handlers run on the regular oid workers. */

typedef struct WellTimer {
    long long id;
    long long interval; /* 0 = once */
    long long deadline;
    long long fires;
    WellTimerFn fn;
    int canceled;
    struct WellTimer *next;
} WellTimer;

typedef struct WellTimerDue {
    long long id;
    long long interval;
    long long fires;
    WellTimerFn fn;
} WellTimerDue;

#define WELL_TIMER_BATCH 16
#define WELL_TIMER_SLEEP_MS 5

static WellMutex g_timer_lock;
static WellCond g_timer_cond;
static int g_timer_inited = 0;
static int g_timer_started = 0;
static int g_timer_stop = 0;
static int g_timer_service = 0; /* service count held while timers exist */
static WellTimer *g_timers = NULL;
static WellThread g_timer_thread;

static void timer_init(void) {
    if (g_timer_inited) return;
    well_mutex_init(&g_timer_lock);
    well_cond_init(&g_timer_cond);
    g_timer_inited = 1;
}

static void timer_info_free(void *arg) {
    free(arg);
}

static void *timer_thread(void *arg) {
    (void)arg;
    for (;;) {
        WellTimerDue due[WELL_TIMER_BATCH];
        int n = 0, i, release_service = 0;
        long long now;
        WellTimer **pp;

        well_mutex_lock(&g_timer_lock);
        if (g_timer_stop) {
            well_mutex_unlock(&g_timer_lock);
            break;
        }
        if (!g_timers) {
            if (g_timer_service) {
                g_timer_service = 0;
                release_service = 1;
            }
            well_mutex_unlock(&g_timer_lock);
            if (release_service) well_service_release();
            well_sleep_ms(WELL_TIMER_SLEEP_MS);
            continue;
        }
        now = well_host_now_ms();
        pp = &g_timers;
        while (*pp && n < WELL_TIMER_BATCH) {
            WellTimer *t = *pp;
            if (t->canceled) {
                *pp = t->next;
                free(t);
                continue;
            }
            if (t->deadline > now) {
                pp = &t->next;
                continue;
            }
            t->fires++;
            due[n].id = t->id;
            due[n].interval = t->interval;
            due[n].fires = t->fires;
            due[n].fn = t->fn;
            n++;
            if (t->interval > 0) {
                /* skip missed ticks instead of bursting */
                t->deadline = now + t->interval;
                pp = &t->next;
            } else {
                *pp = t->next;
                free(t);
            }
        }
        if (!g_timers && g_timer_service) {
            g_timer_service = 0;
            release_service = 1;
        }
        well_mutex_unlock(&g_timer_lock);

        /* Schedule the due handlers before dropping the service count: the
         * task increments `pending`, so the run loop cannot exit in between. */
        for (i = 0; i < n; i++) {
            WellTimerInfo *info = (WellTimerInfo *)malloc(sizeof(WellTimerInfo));
            if (!info) well_panic("out of memory (timer)");
            info->id = due[i].id;
            info->interval = due[i].interval;
            info->fires = due[i].fires;
            well_schedule_task(due[i].id, due[i].fn, info, timer_info_free);
        }
        if (release_service) well_service_release();
        well_sleep_ms(WELL_TIMER_SLEEP_MS);
    }
    return NULL;
}

static void timer_ensure_thread(void) {
    timer_init();
    if (g_timer_started) return;
    g_timer_stop = 0;
    if (well_thread_start(&g_timer_thread, timer_thread, NULL) != 0)
        well_panic("Timer: cannot start timer thread");
    g_timer_started = 1;
}

static long long timer_add(long long ms, long long interval, WellTimerFn fn) {
    WellTimer *t;
    int hold = 0;
    if (!fn) well_panic("Timer: null handler");
    if (ms < 0) ms = 0;
    timer_ensure_thread();
    t = (WellTimer *)calloc(1, sizeof(WellTimer));
    if (!t) well_panic("out of memory (timer)");
    t->id = well_id_util_get_id();
    t->interval = interval;
    t->deadline = well_host_now_ms() + (ms > 0 ? ms : 1);
    t->fires = 0;
    t->fn = fn;
    t->canceled = 0;
    well_mutex_lock(&g_timer_lock);
    if (!g_timer_service) {
        g_timer_service = 1;
        hold = 1;
    }
    t->next = g_timers;
    g_timers = t;
    well_cond_signal(&g_timer_cond);
    well_mutex_unlock(&g_timer_lock);
    if (hold) well_service_hold();
    return t->id;
}

long long well_timer_once(long long ms, WellTimerFn fn) {
    return timer_add(ms, 0, fn);
}

long long well_timer_loop(long long ms, WellTimerFn fn) {
    if (ms <= 0) ms = 1;
    return timer_add(ms, ms, fn);
}

void well_timer_cancel(long long id) {
    WellTimer **pp;
    int release = 0;
    if (!g_timer_inited) return;
    well_mutex_lock(&g_timer_lock);
    pp = &g_timers;
    while (*pp) {
        WellTimer *t = *pp;
        if (t->id == id) {
            *pp = t->next;
            free(t);
            if (!g_timers && g_timer_service) {
                g_timer_service = 0;
                release = 1;
            }
            break;
        }
        pp = &t->next;
    }
    well_mutex_unlock(&g_timer_lock);
    if (release) well_service_release();
}

void well_timer_shutdown(void) {
    if (!g_timer_inited) return;
    well_mutex_lock(&g_timer_lock);
    g_timer_stop = 1;
    while (g_timers) {
        WellTimer *t = g_timers;
        g_timers = t->next;
        free(t);
    }
    if (g_timer_service) {
        g_timer_service = 0;
        well_mutex_unlock(&g_timer_lock);
        well_service_release();
        well_mutex_lock(&g_timer_lock);
    }
    well_cond_signal(&g_timer_cond);
    well_mutex_unlock(&g_timer_lock);
    if (g_timer_started) {
        well_thread_join(g_timer_thread);
        g_timer_started = 0;
    }
    g_timer_stop = 0;
    well_cond_destroy(&g_timer_cond);
    well_mutex_destroy(&g_timer_lock);
    g_timer_inited = 0;
}
