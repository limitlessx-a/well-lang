#include "wellrt.h"
#include <stdio.h>
#include <time.h>

static double run(int n, int mode) {
    WellCtx ctx;
    WellString *key = well_string_new("k");
    WellValue v;
    int i;
    clock_t t0, t1;
    well_ctx_init(&ctx, 1);
    for (i = 0; i < 10000; i++) {
        WellMap *m = well_map_new(&ctx, 0);
        if (mode >= 1) { v.i = i; well_map_set(m, key, v, &ctx); }
        if (mode >= 2) (void)well_map_get(m, key);
        well_release(m);
    }
    t0 = clock();
    for (i = 0; i < n; i++) {
        WellMap *m = well_map_new(&ctx, 0);
        if (mode >= 1) { v.i = i; well_map_set(m, key, v, &ctx); }
        if (mode >= 2) (void)well_map_get(m, key);
        well_release(m);
    }
    t1 = clock();
    well_release(key);
    well_ctx_clear(&ctx);
    return (double)(t1 - t0) * 1e9 / CLOCKS_PER_SEC / n;
}

int main(void) {
    well_init();
    printf("map new+release        %.1f ns\n", run(2000000, 0));
    printf("map new+set+release    %.1f ns\n", run(2000000, 1));
    printf("map new+set+get+release %.1f ns\n", run(2000000, 2));
    well_shutdown();
    return 0;
}
