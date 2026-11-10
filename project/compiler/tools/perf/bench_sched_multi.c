/* Multi-producer scheduler benchmark: N producer threads each enqueue tasks for
 * their own oid while the worker pool drains them.  Contrasts with
 * bench_sched.c, where one producer thread caps W>1. */
#include "wellrt.h"
#include "well_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef TASKS
#define TASKS 300000
#endif
#ifndef PRODUCERS
#define PRODUCERS 4
#endif
#ifndef TASK_WORK
#define TASK_WORK 2000
#endif
#ifndef OID_BASE
#define OID_BASE 1000
#endif

typedef struct {
    int producers;
} SupervisorArg;

static volatile long long g_done = 0;
static long long g_t0 = 0;

static void task_fn(WellCtx *ctx, void *arg) {
    long long i, x = 0;
    (void)ctx;
    (void)arg;
    for (i = 0; i < TASK_WORK; i++) x += i;
    (void)x;
#if defined(_WIN32)
    InterlockedIncrement64((volatile LONG64 *)&g_done);
#else
    __atomic_add_fetch(&g_done, 1, __ATOMIC_RELAXED);
#endif
}

static void *producer_fn(void *arg) {
    long long pid = (long long)(intptr_t)arg;
    long long n = TASKS / PRODUCERS;
    long long i;
    long long oid = OID_BASE + pid;
    for (i = 0; i < n; i++)
        well_schedule_task(oid, task_fn, NULL, NULL);
    return NULL;
}

static void *supervisor_fn(void *arg) {
    SupervisorArg *a = (SupervisorArg *)arg;
    WellThread *ths;
    int i;
    ths = (WellThread *)calloc((size_t)a->producers, sizeof(WellThread));
    if (!ths) well_panic("out of memory (producers)");
    for (i = 0; i < a->producers; i++)
        well_thread_start(&ths[i], producer_fn, (void *)(intptr_t)i);
    for (i = 0; i < a->producers; i++)
        well_thread_join(ths[i]);
    free(ths);
    well_service_release();
    return NULL;
}

int main(void) {
    SupervisorArg a;
    WellThread sup;
    long long ms;
    well_init();
    a.producers = PRODUCERS;
    g_t0 = well_host_now_ms();
    well_service_hold();
    well_thread_start(&sup, supervisor_fn, &a);
    well_run_loop();
    ms = well_host_now_ms() - g_t0;
    well_thread_join(sup);
    printf("tasks %lld producers %d ms %lld tasks_per_sec %lld done %lld\n",
           (long long)TASKS, PRODUCERS, ms,
           (long long)((double)TASKS * 1000.0 / (double)(ms ? ms : 1)), g_done);
    return 0;
}
