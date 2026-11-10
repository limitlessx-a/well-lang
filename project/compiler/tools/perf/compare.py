#!/usr/bin/env python3
"""A/B compare two Well compiler/runtime checkouts on the local benchmarks.

Usage:
  python tools/perf/compare.py \
      --baseline E:\\Work\\WellLang\\project\\compiler \
      --candidate . --clients 8 --seconds 2 --repeat 2

Each scenario builds its server from the given checkout (emit-c + TinyCC
-run), so the runtime under test is exactly that checkout's.  Prints a table and
exits non-zero when the candidate regresses beyond the thresholds.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def run_bench(compiler: str, script: str, extra: list[str], out: str) -> dict:
    env = dict(os.environ)
    env["BENCH_COMPILER"] = compiler
    argv = [sys.executable, os.path.join(HERE, script)] + extra + ["--json", out]
    r = subprocess.run(argv, cwd=os.path.dirname(os.path.dirname(HERE)),
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{script} failed for {compiler}:\n{r.stdout}\n{r.stderr}")
    with open(out, encoding="utf-8") as f:
        return json.load(f)


def fmt_delta(base: float, cur: float, higher_better: bool) -> str:
    if not base:
        return "n/a"
    d = (cur - base) / base * 100.0
    if not higher_better:
        d = -d
    return f"{d:+.1f}%"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--candidate", default=".")
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=2.0)
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--skip-mysql", action="store_true")
    ap.add_argument("--threshold", type=float, default=10.0,
                    help="allowed regression percent (default 10)")
    args = ap.parse_args()
    base_dir = os.path.abspath(args.baseline)
    cand_dir = os.path.abspath(args.candidate)
    tmp = tempfile.mkdtemp(prefix="well-compare-")

    scenarios = [
        ("HTTP", "bench_http.py",
         ["--path", "/bench", "--clients", str(args.clients)]),
        ("WS", "bench_ws.py",
         ["--clients", str(args.clients)]),
    ]
    if not args.skip_mysql:
        scenarios.append(("MySQL", "bench_http.py",
                          ["--source", os.path.join(HERE, "mysql_server.we"),
                           "--path", "/db", "--clients", str(args.clients)]))

    rows = []
    ok = True
    for label, script, extra in scenarios:
        common = extra + ["--seconds", str(args.seconds), "--repeat", str(args.repeat)]
        b = run_bench(base_dir, script, common,
                      os.path.join(tmp, label + "-base.json"))
        c = run_bench(cand_dir, script, common,
                      os.path.join(tmp, label + "-cand.json"))
        for metric, key, higher in (("throughput", "median_rps", True),
                                    ("p50 us", "min_p50_us", False),
                                    ("p99 us", "min_p99_us", False),
                                    ("RSS MB", "max_rss_mb", False)):
            bv, cv = b.get(key, 0), c.get(key, 0)
            delta = fmt_delta(bv, cv, higher)
            regress = False
            if bv and cv:
                d = (cv - bv) / bv * 100.0
                if not higher:
                    d = -d
                regress = d < -args.threshold
            if regress:
                ok = False
            rows.append((f"{label} {metric}", bv, cv, delta, regress))

    print(f"{'scenario':28} {'baseline':>12} {'candidate':>12} {'delta':>9}")
    print("-" * 66)
    for name, bv, cv, delta, regress in rows:
        print(f"{name:28} {bv:12.1f} {cv:12.1f} {delta:>9}{'  REGRESS' if regress else ''}")
    print(f"\nthreshold {args.threshold:.0f}% (p50/p99/RSS lower is better)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
