/* Event dispatch throughput benchmark.
 *
 * Phase 1 enqueues EVENTS events of one type on one oid *before* the scheduler
 * workers start (no producer/consumer lock contention), phase 2 times
 * well_run_loop() draining them to SUBSCRIBERS handlers.  The measured number
 * is therefore dispatch cost, not producer enqueue contention:
 * payload pin, node pool, subscription snapshot, handler dispatch, release.
 *
 * Build (same sources as the C tests):
 *   gcc -O2 -fwrapv -DWELL_NO_OID_CHECK=1 -I runtime/core ... \
 *       tools/perf/bench_event.c <runtime sources> -o bench_event.exe
 */
#include "wellrt.h"
#include "well_internal.h"

#include <stdio.h>
#include <stdlib.h>
#if defined(_WIN32)
#include <windows.h>
static double now_ns(void) {
    LARGE_INTEGER f, c;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart * 1e9 / (double)f.QuadPart;
}
static long g_seen = 0;
static void seen_inc(void) { InterlockedIncrement(&g_seen); }
static long seen_get(void) { return InterlockedCompareExchange(&g_seen, 0, 0); }
#else
#include <time.h>
static double now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1e9 + (double)ts.tv_nsec;
}
static volatile long g_seen = 0;
static void seen_inc(void) { __atomic_add_fetch(&g_seen, 1, __ATOMIC_RELAXED); }
static long seen_get(void) { return __atomic_load_n(&g_seen, __ATOMIC_RELAXED); }
#endif

#ifndef EVENT_TYPE
#define EVENT_TYPE 7001u
#endif
#ifndef SUBSCRIBERS
#define SUBSCRIBERS 2
#endif
#ifndef EVENTS
#define EVENTS 300000
#endif

static void on_event(WellCtx *ctx, void *ev) {
    (void)ctx;
    (void)ev;
    seen_inc();
}

int main(void) {
    double t0, t1, ms;
    int i, s;

    setvbuf(stdout, NULL, _IONBF, 0);
    well_init();
    for (s = 0; s < SUBSCRIBERS; s++)
        well_on_type(EVENT_TYPE, on_event, 0, 0);

    /* Queue everything before any worker exists: workers are started by
     * well_run_loop(), so the enqueue phase is single-threaded and cannot be
     * skewed by producer/consumer lock contention. */
    for (i = 0; i < EVENTS; i++) {
        void *ev = well_new(sizeof(WellObj), EVENT_TYPE, 1);
        well_emit_event(ev, 0);
        well_release(ev); /* emit retained its own edge */
    }

    t0 = now_ns();
    well_run_loop(); /* returns once every event has been dispatched */
    t1 = now_ns();
    ms = (t1 - t0) / 1e6;

    printf("events %d subscribers %d\n", EVENTS, SUBSCRIBERS);
    printf("seen %ld\n", seen_get());
    printf("dispatch_ms %.1f\n", ms);
    printf("events_per_sec %.0f\n", ms > 0 ? EVENTS * 1000.0 / ms : 0.0);
    printf("dispatches_per_sec %.0f\n",
           ms > 0 ? (double)EVENTS * SUBSCRIBERS * 1000.0 / ms : 0.0);
    well_shutdown();
    return 0;
}
