/* 100MB/s allocator/String churn: allocate+copy+release 1MB Strings.
 * Run with the runtime sources via tcc -run; sample process RSS externally. */
#include "wellrt.h"
#include "well_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <time.h>

int main(void) {
    const size_t n = 1024u * 1024u;
    const int secs = 60;
    char *buf;
    clock_t t0;
    long long ops = 0;
    well_init();
    buf = (char *)calloc(1, n);
    if (!buf) return 2;
    t0 = clock();
    while ((double)(clock() - t0) / CLOCKS_PER_SEC < secs) {
        int i;
        for (i = 0; i < 100; i++) {
            WellString *s = well_string_new_n(buf, n);
            well_release(s);
            ops++;
        }
    }
    printf("churn_ops %lld mb_per_s %.1f\n", ops,
           (double)ops * (double)n / (1024.0 * 1024.0) / (double)secs);
    free(buf);
    return 0;
}
