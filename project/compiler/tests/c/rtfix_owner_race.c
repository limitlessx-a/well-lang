/* R-05 regression: the controller owner stack must be per-thread. start()/stop()
 * may run inside handlers on scheduler workers; a process-global stack lets a
 * concurrent worker overwrite the owner, so subscriptions get attributed to the
 * wrong controller. Before the fix thread A read owner 2 (B's) instead of 1.
 */
#include "wellrt.h"
#include "well_internal.h"

#include <stdio.h>

static WellLock g_sync = 0;
static volatile int g_ready_a = 0;
static volatile int g_ready_b = 0;
static int g_seen_by_a = -1;

static void *thread_a(void *arg) {
    (void)arg;
    well_owner_push(1); /* A's controller/owner id */
    well_lock_acquire(&g_sync);
    g_ready_a = 1;
    well_lock_release(&g_sync);
    while (!g_ready_b) well_sleep_ms(0);
    well_sleep_ms(50); /* give B time to push its owner */
    g_seen_by_a = well_owner_current();
    well_owner_pop();
    return NULL;
}

static void *thread_b(void *arg) {
    (void)arg;
    while (!g_ready_a) well_sleep_ms(0);
    well_owner_push(2); /* B's owner id */
    well_lock_acquire(&g_sync);
    g_ready_b = 1;
    well_lock_release(&g_sync);
    well_sleep_ms(200);
    well_owner_pop();
    return NULL;
}

int main(void) {
    WellThread ta, tb;
    int main_view;

    well_init();
    if (well_thread_start(&ta, thread_a, NULL) != 0) return 2;
    if (well_thread_start(&tb, thread_b, NULL) != 0) return 2;
    well_thread_join(ta);
    well_thread_join(tb);
    main_view = well_owner_current();

    /* nesting still works on the calling thread */
    well_owner_push(3);
    well_owner_push(4);
    if (well_owner_current() != 4) main_view = -1;
    well_owner_pop();
    if (well_owner_current() != 3) main_view = -1;
    well_owner_pop();
    if (well_owner_current() != 0) main_view = -1;

    printf("rtfix owner stack tls: seen_by_a=%d main=%d expected=1/0\n",
           g_seen_by_a, main_view);
    return (g_seen_by_a == 1 && main_view == 0) ? 0 : 1;
}
