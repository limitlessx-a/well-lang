/* Runtime primitive microbenchmarks (compiled like the C tests).
 *
 * Measures the pieces that show up in every HTTP/WS/MySQL handler:
 *   new+release object, short/long String, String.fromInt, IdUtil id,
 *   Array push, Map set/get, and event emit+dispatch.
 *
 * Build (from project/compiler):
 *   tcc -I runtime/core -I runtime/modules/http -I runtime/modules/ws \
 *       -I runtime/modules/mysql -o build/perf/rtbench.exe tools/perf/bench_runtime.c \
 *       runtime/core/*.c ... (all module C files)
 */
#include "wellrt.h"
#include "well_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#if defined(_WIN32)
#include <windows.h>
static double now_ns(void) {
    LARGE_INTEGER f, c;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart * 1e9 / (double)f.QuadPart;
}
#else
#include <time.h>
static double now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1e9 + (double)ts.tv_nsec;
}
#endif

#define N 2000000

struct Obj {
    WellObj hdr;
    long long a;
    long long b;
    long long c;
    char pad[40];
};

static void obj_destroy(void *p) {
    well_obj_free(p);
}

static void bench(const char *name, int iters, void (*fn)(int)) {
    double t0, t1;
    fn(iters / 20 > 1000 ? iters / 20 : 1000); /* warmup */
    t0 = now_ns();
    fn(iters);
    t1 = now_ns();
    printf("%-24s %10.2f ns/op  (%d ops, %.1f ms)\n",
           name, (t1 - t0) / (double)iters, iters, (t1 - t0) / 1e6);
}

static void b_obj(int n) {
    int i;
    for (i = 0; i < n; i++) {
        struct Obj *o = (struct Obj *)well_new(sizeof(*o), 900, 1);
        o->a = i;
        o->b = i + 1;
        o->c = i + 2;
        well_release(o);
    }
}

static void b_string_short(int n) {
    int i;
    for (i = 0; i < n; i++) {
        WellString *s = well_string_new("abcdefgh");
        well_release(s);
    }
}

static void b_string_int(int n) {
    int i;
    for (i = 0; i < n; i++) {
        WellString *s = well_str_from_int(i);
        well_release(s);
    }
}

static void b_id(int n) {
    int i;
    long long acc = 0;
    for (i = 0; i < n; i++) acc += well_id_util_get_id();
    if (acc == 42) printf("x");
}

static void b_registry_only(int n) {
    int i;
    for (i = 0; i < n; i++) {
        struct Obj *o = (struct Obj *)well_new(sizeof(*o), 900, 1);
        well_obj_free(o); /* skip rc/registry removal path */
    }
}

static void b_array(int n) {
    int i;
    WellCtx ctx;
    well_ctx_init(&ctx, 1);
    for (i = 0; i < n; i++) {
        WellArray *a = well_array_new(&ctx, 0);
        well_array_push(a, well_box_i(i), &ctx);
        well_release(a);
    }
}

int main(int argc, char **argv) {
    int n = N;
    (void)argc;
    (void)argv;
    setvbuf(stdout, NULL, _IONBF, 0);
    well_init();
    well_register_type(900, obj_destroy);
    printf("runtime primitive benchmarks: %d ops each\n", n);
    bench("object new+release", n, b_obj);
    bench("object alloc/free only", n, b_registry_only);
    bench("String 8B new+release", n, b_string_short);
    bench("String.fromInt", n, b_string_int);
    bench("IdUtil.getId", n, b_id);
    bench("Array new+push+release", n, b_array);
    well_shutdown();
    return 0;
}
