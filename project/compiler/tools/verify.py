#!/usr/bin/env python3
"""One-command correctness regression for the Well compiler.

Usage:
    python tools/verify.py            # all .we cases + C tests
    python tools/verify.py --quick    # all .we cases only
    python tools/verify.py --full     # all tests + a short HTTP bench smoke
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
COMPILER_DIR = os.path.dirname(HERE)
sys.path.insert(0, COMPILER_DIR)
sys.path.insert(0, os.path.join(COMPILER_DIR, "tests"))

import run_tests  # noqa: E402
from wellc.driver import find_cc, cc_flags  # noqa: E402


def check_toolchain() -> bool:
    print(f"python: {sys.version.split()[0]} ({sys.executable})")
    try:
        cc = find_cc()
    except Exception as e:  # noqa: BLE001
        print(f"toolchain: FAIL ({e})")
        return False
    if not os.path.isfile(cc) and not os.path.exists(cc):
        print(f"toolchain: FAIL (C compiler not found: {cc})")
        return False
    print(f"cc:     {cc}")
    flags = cc_flags()
    print(f"cflags: {' '.join(flags) if flags else '(none)'}")
    return True


def run_we_cases() -> tuple[int, int]:
    names = sorted(
        n for n in os.listdir(run_tests.CASES)
        if os.path.isdir(os.path.join(run_tests.CASES, n))
    )
    ok = 0
    for n in names:
        if run_tests.run_case(n):
            ok += 1
    return ok, len(names)


def main() -> int:
    ap = argparse.ArgumentParser(description="Well compiler regression runner")
    ap.add_argument("--quick", action="store_true", help="only .we cases (skip C tests/bench)")
    ap.add_argument("--full", action="store_true", help="all tests plus a short bench smoke")
    ap.add_argument("--bench", action="store_true", help="alias for --full")
    args = ap.parse_args()

    t0 = time.time()
    if not check_toolchain():
        return 2

    ok, total = run_we_cases()
    c_ok = True
    if not args.quick:
        c_ok = run_tests.run_c_tests()

    bench_ok = True
    if args.full or args.bench:
        bench = os.path.join(HERE, "bench.py")
        print("\n--- bench smoke ---")
        r = subprocess.run(
            [sys.executable, bench, "--scenario", "http",
             "--iterations", "200", "--clients", "2", "--seconds", "1"],
            cwd=COMPILER_DIR,
        )
        bench_ok = r.returncode == 0

    elapsed = time.time() - t0
    good = ok == total and c_ok and bench_ok
    print(
        f"\nverify: {ok}/{total} cases"
        + ("" if args.quick else (", C tests passed" if c_ok else ", C tests FAILED"))
        + ("" if bench_ok else ", bench FAILED")
        + f", {elapsed:.1f}s -> {'OK' if good else 'FAILED'}"
    )
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
