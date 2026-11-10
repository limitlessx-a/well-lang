#include <cstdio>
#include <cstdlib>
#include <chrono>
#include <vector>

int main(int argc, char **argv) {
    long long n = argc > 1 ? std::atoll(argv[1]) : 2000000LL;
    volatile long long sink = 0;
    auto t0 = std::chrono::steady_clock::now();
    for (long long i = 0; i < n; i++) {
        auto *v = new std::vector<long long>();
        v->push_back(i);
        sink += v->size();
        delete v;
    }
    double ms = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - t0).count();
    std::printf("cpp-vec n=%lld ms=%.1f ns_per_op=%.1f sink=%lld\n",
                n, ms, ms * 1e6 / (double)n, (long long)sink);
    return 0;
}
