/* R-03 regression: well_string_lit() interns string literals in a process-wide
 * table. Generated code calls it inline, including from scheduler worker
 * threads, so the first registration of distinct literals happens concurrently.
 * Before the fix the unlocked table hit concurrent realloc()/append and
 * corrupted the heap (STATUS_HEAP_CORRUPTION / access violation). The check
 * after joining also proves that interning stayed exact: every literal must
 * come back as the same WellString pointer.
 */
#include "wellrt.h"
#include "well_internal.h"

#include <stdio.h>
#include <string.h>

#define NTHREADS 8
#define NITER 2000

static WellLock g_start_lock = 0;
static volatile int g_ready = 0;
static volatile int g_go = 0;
static char *g_out[NTHREADS][NITER / 8];

static void *lit_worker(void *arg) {
    int id = (int)(long)arg;
    char buf[64];
    int i;
    well_lock_acquire(&g_start_lock);
    g_ready++;
    well_lock_release(&g_start_lock);
    while (!g_go) well_sleep_ms(0);
    for (i = 0; i < NITER; i++) {
        WellString *s;
        sprintf(buf, "rtfix_lit_%d_%d", id, i);
        s = well_string_lit(buf);
        if (!s || s->len <= 0) return (void *)1;
        if ((i & 7) == 0) g_out[id][i / 8] = (char *)well_string_cstr(s);
    }
    return NULL;
}

int main(void) {
    WellThread t[NTHREADS];
    int i, bad = 0;
    char expect[64];

    well_init();
    for (i = 0; i < NTHREADS; i++) {
        if (well_thread_start(&t[i], lit_worker, (void *)(long)i) != 0) return 2;
    }
    while (g_ready < NTHREADS) well_sleep_ms(0);
    g_go = 1;
    for (i = 0; i < NTHREADS; i++) well_thread_join(t[i]);
    for (i = 0; i < NTHREADS; i++) {
        int j;
        for (j = 0; j < NITER / 8; j++) {
            WellString *s;
            sprintf(expect, "rtfix_lit_%d_%d", i, j * 8);
            s = well_string_lit(expect);
            if ((char *)well_string_cstr(s) != g_out[i][j]) bad++;
        }
    }
    printf("rtfix string literal race: bad=%d\n", bad);
    return bad ? 1 : 0;
}
