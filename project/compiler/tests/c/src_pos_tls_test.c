/* Source-position TLS contract (v3 lesson, P1).
 *
 * Generated code calls well_source_set() before every potentially panicking
 * statement.  The position must be per-thread: two workers running generated
 * code concurrently must never observe each other's file/line.  The check
 * starts 20 concurrent pairs, has every thread write a unique position, then
 * waits until all are written and verifies each thread still reads back its
 * own position.
 */
#include "wellrt.h"
#include "well_internal.h"

#include <stdio.h>

#define PAIRS 20

typedef struct {
    int id;
    char file[64];
    int line;
    int errors;
} SrcArg;

static WellLock g_lock = 0;
static volatile int g_ready = 0;

static int src_slot_id(int id) { return id * 2; }

static void *src_worker(void *p) {
    SrcArg *a = (SrcArg *)p;
    int i;
    for (i = 0; i < 8; i++) {
        const char *got_file;
        int got_line;
        well_source_set(a->file, a->line);
        well_lock_acquire(&g_lock);
        g_ready++;
        well_lock_release(&g_lock);
        while (g_ready < (i + 1) * PAIRS * 2) well_sleep_ms(0);
        got_file = well_source_file();
        got_line = well_source_line();
        if (!got_file || got_line != a->line || got_file != a->file)
            a->errors++;
    }
    return NULL;
}

int main(void) {
    SrcArg args[PAIRS * 2];
    WellThread threads[PAIRS * 2];
    int i, errors = 0;

    well_init();
    for (i = 0; i < PAIRS * 2; i++) {
        args[i].id = i;
        snprintf(args[i].file, sizeof(args[i].file), "worker_%02d.we", i);
        args[i].line = src_slot_id(i) + 1000;
        args[i].errors = 0;
    }
    for (i = 0; i < PAIRS * 2; i++)
        if (well_thread_start(&threads[i], src_worker, &args[i]) != 0) {
            printf("src pos tls: cannot start thread %d\n", i);
            return 1;
        }
    for (i = 0; i < PAIRS * 2; i++) well_thread_join(threads[i]);
    for (i = 0; i < PAIRS * 2; i++) errors += args[i].errors;
    /* Main thread keeps its own slot too. */
    well_source_set("main.we", 7);
    if (well_source_line() != 7 || well_source_file() == NULL) errors++;
    printf("src pos tls ok pairs=%d errors=%d\n", PAIRS, errors);
    return errors == 0 ? 0 : 1;
}
