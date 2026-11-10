#include <cstdio>
#include <cstdlib>
#include <chrono>
#include <string>
#include <vector>

int main(int argc, char **argv) {
    long long n = argc > 1 ? std::atoll(argv[1]) : 2000000LL;
    volatile long long sink = 0;
    auto t0 = std::chrono::steady_clock::now();
    for (long long i = 0; i < n; i++) {
        auto *a = new std::vector<std::string>{"a", "bb"};
        sink += (long long)((*a)[0].size() + (*a)[1].size());
        delete a;
    }
    double ms = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - t0).count();
    std::printf("cpp-arrstr n=%lld ms=%.1f ns_per_op=%.1f sink=%lld\n",
                n, ms, ms * 1e6 / (double)n, (long long)sink);
    return 0;
}
