#include <stdio.h>
#include <stdlib.h>
#include <time.h>

int main(int argc, char **argv) {
    long long n = argc > 1 ? atoll(argv[1]) : 20000000LL;
    long long a[1024];
    long long acc = 0;
    int i;
    for (i = 0; i < 1024; i++) a[i] = i;
    clock_t t0 = clock();
    for (i = 0; i < n; i++) acc += a[i & 1023];
    double ms = (double)(clock() - t0) * 1000.0 / CLOCKS_PER_SEC;
    printf("c-arr n=%lld ms=%.1f ns_per_iter=%.3f acc=%lld\n",
           n, ms, ms * 1e6 / (double)n, acc);
    return 0;
}
