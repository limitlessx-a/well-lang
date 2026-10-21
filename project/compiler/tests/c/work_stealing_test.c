#include "wellrt.h"
#include "well_internal.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>

typedef struct Ev {
    WellObj hdr;
} Ev;

#define N 500
static WellMutex g_mu;
static long long g_count = 0;

static void on_event(WellCtx *ctx, void *evp) {
    (void)ctx; (void)evp;
    well_mutex_lock(&g_mu);
    g_count++;
    well_mutex_unlock(&g_mu);
}

static void ev_destroy(void *p) { free(p); }

int main(void) {
    int i;
#ifdef _WIN32
    _putenv_s("WELL_WORKERS", "4");
#else
    setenv("WELL_WORKERS", "4", 1);
#endif

    well_init();
    well_register_type(900, ev_destroy);
    well_mutex_init(&g_mu);
    well_on_type(900, on_event, 0, 0);

    for (i = 1; i <= N; i++) {
        Ev *e = (Ev *)well_new(sizeof(Ev), 900, i);
        well_emit_event(e, 0);
        well_release(e);
    }
    well_run_loop();

    assert(g_count == N);
    well_off_type(900, on_event, 0);
    well_mutex_destroy(&g_mu);
    well_shutdown();
    printf("work stealing ok\n");
    return 0;
}
