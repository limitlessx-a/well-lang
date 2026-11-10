/* Scheduler throughput benchmark.
 *
 * A producer thread schedules N tasks across a small set of oids while the
 * workers drain them; the run loop exits when all tasks are done.  This is the
 * path used by emit()/timers (HTTP/WS run handlers inline by default).
 *
 * Build/run (TinyCC):
 *   tcc -I runtime/core ... tools/perf/bench_sched.c <runtime sources> -run
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
static long g_done = 0;
static void done_inc(void) { InterlockedIncrement(&g_done); }
static long done_get(void) { return InterlockedCompareExchange(&g_done, 0, 0); }
#else
#include <time.h>
static double now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1e9 + (double)ts.tv_nsec;
}
static volatile long g_done = 0;
static void done_inc(void) { __atomic_add_fetch(&g_done, 1, __ATOMIC_RELAXED); }
static long done_get(void) { return __atomic_load_n(&g_done, __ATOMIC_RELAXED); }
#endif

#ifndef TASK_WORK
#define TASK_WORK 0
#endif
#define TASKS 300000
#ifndef OIDS
#define OIDS 64
#endif

static volatile long long g_sink;
static void task_fn(WellCtx *ctx, void *arg) {
    long long i, x = 0;
    (void)ctx;
    (void)arg;
    for (i = 0; i < TASK_WORK; i++) x += i;
    g_sink += x;
    done_inc();
}

typedef struct {
    long long t0;
    long long t1;
} ProducerResult;

static ProducerResult g_prod;

static void *producer(void *arg) {
    int n = (int)(long long)arg;
    int i;
    well_sleep_ms(200); /* let the workers enter their wait loop */
    g_prod.t0 = (long long)now_ns();
    for (i = 0; i < n; i++) {
        long long oid = (long long)(i % OIDS) + 1;
        well_schedule_task(oid, task_fn, NULL, NULL);
    }
    g_prod.t1 = (long long)now_ns();
    well_service_release();
    return NULL;
}

int main(void) {
    WellThread th;
    long long t_start, t_end;
    double total_ms, sched_ms;
    setvbuf(stdout, NULL, _IONBF, 0);
    well_init();
    well_service_hold();
    t_start = (long long)now_ns();
    if (well_thread_start(&th, producer, (void *)(long long)TASKS) != 0) {
        printf("cannot start producer\n");
        return 1;
    }
    well_run_loop();
    t_end = (long long)now_ns();
    well_thread_join(th);
    total_ms = (double)(t_end - t_start) / 1e6;
    sched_ms = (double)(g_prod.t1 - g_prod.t0) / 1e6;
    printf("tasks %d oids %d\n", TASKS, OIDS);
    printf("done %ld\n", done_get());
    printf("sched_ms %.1f\n", sched_ms);
    printf("total_ms %.1f\n", total_ms);
    printf("drain_ms %.1f\n", total_ms - sched_ms);
    printf("tasks_per_sec %.0f\n", total_ms > 0 ? TASKS * 1000.0 / total_ms : 0.0);
    printf("sched_per_sec %.0f\n", sched_ms > 0 ? TASKS * 1000.0 / sched_ms : 0.0);
    well_shutdown();
    return 0;
}
