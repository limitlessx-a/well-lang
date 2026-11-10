#!/usr/bin/env python3
"""Formal HTTP/WS A/B: N independent processes x R rounds, first W warm-ups.

The v3 experiment method asks for 5 independent processes x 7 rounds with the
first 2 rounds discarded, alternating variants so slow drift (thermal, other
load) hits both sides equally.  bench_http.py/bench_ws.py already run their own
rounds inside one process; this driver runs that whole script once per process
and aggregates the per-round numbers.

Example:
    python tools/perf/formal_ab.py --kind http \
        --a "v2=D:\\Work\\WellLangV4\\project\\tool_ai\\baseline\\project\\compiler" \
        --b "v4=D:\\Work\\WellLangV4\\project\\compiler" \
        --processes 5 --rounds 7 --warmup 2 --seconds 3 \
        --json build/perf/formal_http.json
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile


def summarize(values):
    s = sorted(values)
    return {
        "n": len(s),
        "min": round(s[0], 2),
        "median": round(statistics.median(s), 2),
        "max": round(s[-1], 2),
        "mean": round(statistics.fmean(s), 2),
    }


def run_script(tree: str, kind: str, args, out_json: str) -> dict:
    script = os.path.join(tree, "tools", "perf", f"bench_{kind}.py")
    cmd = [sys.executable, script, "--repeat", str(args.rounds),
           "--seconds", str(args.seconds), "--clients", str(args.clients),
           "--workers", str(args.workers), "--iterations", str(args.iterations),
           "--json", out_json]
    if args.release:
        cmd.append("--release")
    r = subprocess.run(cmd, cwd=tree, capture_output=True, text=True,
                       timeout=args.timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{script} failed ({r.returncode}):\n"
                           f"{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    with open(out_json, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="formal HTTP/WS A/B")
    ap.add_argument("--kind", choices=["http", "ws"], default="http")
    ap.add_argument("--a", required=True, metavar="NAME=TREE")
    ap.add_argument("--b", required=True, metavar="NAME=TREE")
    ap.add_argument("--processes", type=int, default=5)
    ap.add_argument("--rounds", type=int, default=7)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--iterations", type=int, default=1500)
    ap.add_argument("--release", action="store_true",
                    help="build the server with --release (GCC/Clang -O2)")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    variants = []
    for spec in (args.a, args.b):
        name, tree = spec.split("=", 1)
        variants.append((name, tree))

    rps = {name: [] for name, _ in variants}
    p50 = {name: [] for name, _ in variants}
    p99 = {name: [] for name, _ in variants}
    raw = []

    tmpdir = tempfile.mkdtemp(prefix="formal-ab-")
    for proc in range(args.processes):
        order = variants if proc % 2 == 0 else list(reversed(variants))
        for name, tree in order:
            out = os.path.join(tmpdir, f"{name}-{proc}.json")
            data = run_script(tree, args.kind, args, out)
            runs = data.get("runs", [])
            kept = runs[args.warmup:]
            for r in kept:
                rps[name].append(r["rps"])
                p50[name].append(r["latency_p50_us"])
                p99[name].append(r["latency_p99_us"])
            raw.append({
                "process": proc,
                "variant": name,
                "tree": tree,
                "runs": runs,
                "kept": len(kept),
                "median_rps": data.get("median_rps"),
                "min_p50_us": data.get("min_p50_us"),
                "min_p99_us": data.get("min_p99_us"),
                "max_rss_mb": data.get("max_rss_mb"),
            })
            print(f"process {proc + 1}/{args.processes} {name:6s} "
                  f"kept={len(kept)} median_rps={data.get('median_rps')} "
                  f"p50={data.get('min_p50_us')}us p99={data.get('min_p99_us')}us",
                  flush=True)

    result = {"kind": args.kind, "processes": args.processes,
              "rounds": args.rounds, "warmup": args.warmup,
              "variants": {}, "raw": raw}
    print()
    print(f"{'variant':8s} {'rps min':>12s} {'median':>12s} {'max':>12s} "
          f"{'p50 med':>9s} {'p99 med':>9s}")
    for name, _tree in variants:
        st = summarize(rps[name])
        result["variants"][name] = {
            "rps": st,
            "p50_us": summarize(p50[name]),
            "p99_us": summarize(p99[name]),
            "samples_rps": rps[name],
        }
        print(f"{name:8s} {st['min']:12.0f} {st['median']:12.0f} "
              f"{st['max']:12.0f} "
              f"{statistics.median(p50[name]):9.2f} "
              f"{statistics.median(p99[name]):9.2f}")

    a_name, b_name = variants[0][0], variants[1][0]
    base = result["variants"][a_name]["rps"]["median"]
    other = result["variants"][b_name]["rps"]["median"]
    if base:
        print(f"\n{b_name} vs {a_name}: {(other - base) / base * 100:+.1f}% "
              f"(median {other:.0f} vs {base:.0f} rps)")

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
