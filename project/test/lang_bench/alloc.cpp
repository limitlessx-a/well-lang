#include <cstdio>
#include <cstdlib>
#include <chrono>

static volatile char *g_sink;

int main(int argc, char **argv) {
    long long n = argc > 1 ? std::atoll(argv[1]) : 2000000LL;
    auto t0 = std::chrono::steady_clock::now();
    for (long long i = 0; i < n; i++) {
        char *p = new char[64];
        p[0] = (char)i;
        g_sink = p;
        delete[] p;
    }
    double ms = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - t0).count();
    std::printf("cpp alloc n=%lld ms=%.1f ns_per_op=%.1f\n",
                n, ms, ms * 1e6 / (double)n);
    return 0;
}
