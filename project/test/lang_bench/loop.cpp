#include <cstdio>
#include <cstdlib>
#include <chrono>

int main(int argc, char **argv) {
    long long n = argc > 1 ? std::atoll(argv[1]) : 100000000LL;
    long long acc = 0;
    auto t0 = std::chrono::steady_clock::now();
    for (long long i = 0; i < n; i++) acc += i;
    auto d = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - t0).count();
    std::printf("cpp n=%lld ms=%.1f ns_per_iter=%.3f acc=%lld\n",
                n, d, d * 1e6 / (double)n, acc);
    return 0;
}
