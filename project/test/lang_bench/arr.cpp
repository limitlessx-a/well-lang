#include <cstdio>
#include <cstdlib>
#include <chrono>

int main(int argc, char **argv) {
    long long n = argc > 1 ? std::atoll(argv[1]) : 20000000LL;
    long long a[1024];
    long long acc = 0;
    for (int i = 0; i < 1024; i++) a[i] = i;
    auto t0 = std::chrono::steady_clock::now();
    for (long long i = 0; i < n; i++) acc += a[i & 1023];
    double ms = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - t0).count();
    std::printf("cpp-arr n=%lld ms=%.1f ns_per_iter=%.3f acc=%lld\n",
                n, ms, ms * 1e6 / (double)n, acc);
    return 0;
}
