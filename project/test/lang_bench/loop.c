#include <stdio.h>
#include <stdlib.h>
#include <time.h>

int main(int argc, char **argv) {
    long long n = argc > 1 ? atoll(argv[1]) : 100000000LL;
    long long acc = 0, i;
    clock_t t0 = clock();
    for (i = 0; i < n; i++) acc += i;
    double ms = (double)(clock() - t0) * 1000.0 / CLOCKS_PER_SEC;
    printf("c n=%lld ms=%.1f ns_per_iter=%.3f acc=%lld\n",
           n, ms, ms * 1e6 / (double)n, acc);
    return 0;
}
