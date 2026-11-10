#include "wellrt.h"
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

int main(void) {
    int n = 2000000;
    int i;
    WellCtx ctx;
    WellString *key = well_string_new("k");
    WellValue v;
    clock_t t0, t1;
    well_init();
    well_ctx_init(&ctx, 1);
    for (i = 0; i < 10000; i++) {
        WellMap *m = well_map_new(&ctx, 0);
        v.i = i;
        well_map_set(m, key, v, &ctx);
        (void)well_map_get(m, key);
        well_release(m);
    }
    t0 = clock();
    for (i = 0; i < n; i++) {
        WellMap *m = well_map_new(&ctx, 0);
        v.i = i;
        well_map_set(m, key, v, &ctx);
        (void)well_map_get(m, key);
        well_release(m);
    }
    t1 = clock();
    printf("map new+set+get+release: %.1f ns/op (%d ops, %.0f ms)\n",
           (double)(t1 - t0) * 1e9 / CLOCKS_PER_SEC / n, n,
           (double)(t1 - t0) * 1000 / CLOCKS_PER_SEC);
    well_release(key);
    well_ctx_clear(&ctx);
    well_shutdown();
    return 0;
}
