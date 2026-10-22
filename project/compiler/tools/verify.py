#!/usr/bin/env python3
"""One-command correctness regression for the Well compiler.

Usage:
    python tools/verify.py            # all .we cases + C tests
    python tools/verify.py --quick    # all .we cases only
    python tools/verify.py --full     # all tests + a short HTTP bench smoke
"""
import argparse
import json
import os
import shutil
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
    # WELL_CC may be a bare command name ("gcc"); resolve it through PATH
    # before the file-existence check.
    if not os.path.isabs(cc):
        resolved = shutil.which(cc)
        if resolved:
            cc = resolved
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


def check_cli() -> bool:
    """Fast smoke test for `python wellc.py check --json FILE`.

    Runs in --quick and --full so CI catches a broken diagnostics contract.
    Uses sys.executable (not a bare `python`) so it works on any machine.
    """
    wellc = os.path.join(COMPILER_DIR, "wellc.py")
    examples = os.path.join(COMPILER_DIR, "examples", "lib", "http_parser.we")
    cases = [
        # path, want ok, want exit, needs a positioned error
        (os.path.join(run_tests.CASES, "01_hello", "main.we"), True, 0, False),
        (os.path.join(run_tests.CASES, "08_compile_error", "main.we"), False, 1, True),
        (os.path.join(run_tests.CASES, "11_bad_event", "main.we"), False, 1, False),
        (examples, True, 0, False),
    ]
    for path, want_ok, want_exit, want_loc in cases:
        label = os.path.relpath(path, COMPILER_DIR)
        try:
            r = subprocess.run(
                [sys.executable, wellc, "check", "--json", path],
                cwd=COMPILER_DIR, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as e:
            print(f"FAIL check-cli: {label}: cannot run wellc.py check: {e}")
            return False
        if r.returncode != want_exit:
            print(f"FAIL check-cli: {label}: exit {r.returncode}, want {want_exit}"
                  f" (stderr: {r.stderr.strip()[:300]})")
            return False
        try:
            data = json.loads(r.stdout)
        except ValueError:
            print(f"FAIL check-cli: {label}: stdout is not JSON: {r.stdout[:300]!r}")
            return False
        if not isinstance(data, dict) or "ok" not in data or "diagnostics" not in data:
            print(f"FAIL check-cli: {label}: malformed JSON object: {str(data)[:300]}")
            return False
        if data["ok"] is not want_ok:
            print(f"FAIL check-cli: {label}: ok={data['ok']!r}, want {want_ok!r}")
            return False
        if not isinstance(data["diagnostics"], list):
            print(f"FAIL check-cli: {label}: diagnostics is not a list")
            return False
        errors = [d for d in data["diagnostics"] if d.get("severity") == "error"]
        if (want_ok and errors) or (not want_ok and not errors):
            print(f"FAIL check-cli: {label}: error diagnostics mismatch "
                  f"({len(errors)} errors)")
            return False
        if want_loc:
            for d in errors:
                line, col = d.get("line"), d.get("col")
                end_line, end_col = d.get("endLine"), d.get("endCol")
                if not all(isinstance(v, int) and v >= 1
                           for v in (line, col, end_line, end_col)):
                    print(f"FAIL check-cli: {label}: error without a valid "
                          f"line/col range: {d}")
                    return False
    print("check-cli: OK")
    return True


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
    cli_ok = check_cli()
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
    good = ok == total and c_ok and bench_ok and cli_ok
    skipped_names = list(run_tests.SKIPPED)
    ran = ok - len(skipped_names)
    print(
        f"\nverify: {ran}/{total - len(skipped_names)} cases passed"
        + (f", {len(skipped_names)} skipped: {', '.join(skipped_names)}" if skipped_names else "")
        + ("" if args.quick else (", C tests passed" if c_ok else ", C tests FAILED"))
        + ("" if bench_ok else ", bench FAILED")
        + f", {elapsed:.1f}s -> {'OK' if good else 'FAILED'}"
    )
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
