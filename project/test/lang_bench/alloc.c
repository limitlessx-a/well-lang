#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static volatile char *g_sink;

int main(int argc, char **argv) {
    long long n = argc > 1 ? atoll(argv[1]) : 2000000LL;
    long long i;
    clock_t t0 = clock();
    for (i = 0; i < n; i++) {
        char *p = (char *)malloc(64);
        p[0] = (char)i;
        g_sink = p;
        free(p);
    }
    double ms = (double)(clock() - t0) * 1000.0 / CLOCKS_PER_SEC;
    printf("c alloc n=%lld ms=%.1f ns_per_op=%.1f\n",
           n, ms, ms * 1e6 / (double)n);
    return 0;
}
