#include "wellrt.h"
#include <stdio.h>
#include <time.h>

struct Obj { WellObj hdr; int a; int b; long long c; };
static void obj_destroy(void *p) { well_obj_free(p); }

int main(void) {
    int n = 2000000, i;
    clock_t t0, t1;
    well_init();
    well_register_type(900, obj_destroy);
    for (i = 0; i < 10000; i++) {
        struct Obj *o = (struct Obj *)well_new_uninit_unregistered(sizeof(*o), 900, 1);
        o->a = i; o->b = i + 1; o->c = i + 2;
        well_release(o);
    }
    t0 = clock();
    for (i = 0; i < n; i++) {
        struct Obj *o = (struct Obj *)well_new_uninit_unregistered(sizeof(*o), 900, 1);
        o->a = i; o->b = i + 1; o->c = i + 2;
        well_release(o);
    }
    t1 = clock();
    printf("scalar-heap-unregistered: %.1f ns/op (%d ops, %.0f ms)\n",
           (double)(t1 - t0) * 1e9 / CLOCKS_PER_SEC / n, n,
           (double)(t1 - t0) * 1000 / CLOCKS_PER_SEC);
    well_shutdown();
    return 0;
}
