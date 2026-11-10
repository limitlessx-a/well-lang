#include "wellrt.h"
#include <stdio.h>
#include <time.h>

static double run(int n, int flat) {
    WellCtx ctx;
    WellString *s = well_string_lit("x");
    WellValue v;
    int i;
    clock_t t0, t1;
    well_ctx_init(&ctx, 1);
    v.p = s;
    for (i = 0; i < 10000; i++) {
        WellArray *a = flat ? well_array_new_flat(&ctx, 1) : well_array_new(&ctx, 1);
        well_array_push(a, v, &ctx);
        well_release(a);
    }
    t0 = clock();
    for (i = 0; i < n; i++) {
        WellArray *a = flat ? well_array_new_flat(&ctx, 1) : well_array_new(&ctx, 1);
        well_array_push(a, v, &ctx);
        well_release(a);
    }
    t1 = clock();
    well_ctx_clear(&ctx);
    return (double)(t1 - t0) * 1e9 / CLOCKS_PER_SEC / n;
}

int main(void) {
    well_init();
    printf("Array<String> registered %.1f ns\n", run(2000000, 0));
    printf("Array<String> flat       %.1f ns\n", run(2000000, 1));
    well_shutdown();
    return 0;
}
