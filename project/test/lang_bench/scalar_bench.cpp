#include <cstdio>
#include <cstdlib>
#include <chrono>

struct Point { int x; int y; long long z; };

int main(int argc, char **argv) {
    long long n = argc > 1 ? std::atoll(argv[1]) : 2000000LL;
    volatile long long sink = 0;
    auto t0 = std::chrono::steady_clock::now();
    for (long long i = 0; i < n; i++) {
        Point *p = new Point{1, 2, i};
        sink += p->x + p->y + p->z;
        delete p;
    }
    double ms = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - t0).count();
    std::printf("cpp-scalar n=%lld ms=%.1f ns_per_op=%.1f sink=%lld\n",
                n, ms, ms * 1e6 / (double)n, (long long)sink);
    return 0;
}
