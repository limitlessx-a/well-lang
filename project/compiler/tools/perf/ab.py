#!/usr/bin/env python3
"""Interleaved A/B benchmark runner (v3 experiment method, P3).

Every variant is an executable that prints one metric (for example
``ns_per_iter 12.34``).  The runner starts ``--rounds`` independent processes
per variant, rotates the start order each round, drops the first ``--warmup``
rounds, and reports min / median / max together with the raw samples.

Keeping the previous binary and alternating A/B in the same session is what
makes a 3-5% difference measurable; a single run cannot.

Example:
    python tools/perf/ab.py \
        --run tcc=build/perf/alloc_tcc.exe \
        --run gcc=build/perf/alloc_gcc.exe \
        --metric "ns_per_iter\\s+([0-9.]+)" \
        --rounds 7 --warmup 2 --json build/perf/alloc_ab.json
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time


def run_once(exe: str, timeout: float) -> tuple[str, float]:
    t0 = time.perf_counter()
    r = subprocess.run([exe], capture_output=True, text=True, timeout=timeout)
    elapsed = time.perf_counter() - t0
    if r.returncode != 0:
        raise RuntimeError(f"{exe} exited {r.returncode}:\n{r.stdout}\n{r.stderr}")
    return r.stdout, elapsed


def parse_metric(out: str, pattern: re.Pattern) -> float:
    m = pattern.search(out)
    if not m:
        raise RuntimeError(f"metric {pattern.pattern!r} not found in output:\n{out[:800]}")
    return float(m.group(1))


def summarize(samples: list[float]) -> dict:
    s = sorted(samples)
    return {
        "n": len(s),
        "min": round(s[0], 4),
        "median": round(statistics.median(s), 4),
        "max": round(s[-1], 4),
        "mean": round(statistics.fmean(s), 4),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="interleaved A/B benchmark runner")
    ap.add_argument("--run", action="append", default=[], metavar="NAME=EXE",
                    help="variant executable (repeatable)")
    ap.add_argument("--metric", required=True,
                    help="regex with one capture group extracting the metric")
    ap.add_argument("--rounds", type=int, default=7)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--json", default=None, help="write raw + summary JSON here")
    ap.add_argument("--label", default="")
    args = ap.parse_args(argv)

    if len(args.run) < 2:
        ap.error("need at least two --run NAME=EXE variants")
    variants: list[tuple[str, str]] = []
    for spec in args.run:
        if "=" not in spec:
            ap.error(f"bad --run {spec!r}; expected NAME=EXE")
        name, exe = spec.split("=", 1)
        if not os.path.isfile(exe):
            ap.error(f"missing executable for {name}: {exe}")
        variants.append((name, exe))

    pattern = re.compile(args.metric)
    samples: dict[str, list[float]] = {name: [] for name, _ in variants}
    wall: dict[str, list[float]] = {name: [] for name, _ in variants}
    raw: list[dict] = []

    for rnd in range(args.rounds):
        order = variants[rnd % len(variants):] + variants[:rnd % len(variants)]
        for name, exe in order:
            out, elapsed = run_once(exe, args.timeout)
            value = parse_metric(out, pattern)
            samples[name].append(value)
            wall[name].append(elapsed)
            raw.append({"round": rnd, "variant": name, "metric": value,
                        "wall_s": round(elapsed, 3)})
            print(f"round {rnd + 1}/{args.rounds} {name:12s} {value:12.4f} "
                  f"(wall {elapsed:6.2f}s)", flush=True)

    print()
    print(f"{'variant':14s} {'min':>10s} {'median':>10s} {'max':>10s} {'mean':>10s}")
    results = {}
    for name, _exe in variants:
        kept = samples[name][args.warmup:]
        st = summarize(kept)
        results[name] = {"summary": st,
                         "samples_all": samples[name],
                         "samples_kept": kept,
                         "wall_s": wall[name]}
        print(f"{name:14s} {st['min']:10.2f} {st['median']:10.2f} "
              f"{st['max']:10.2f} {st['mean']:10.2f}")

    baseline = variants[0][0]
    if len(variants) > 1:
        base_med = results[baseline]["summary"]["median"]
        print()
        for name, _exe in variants[1:]:
            med = results[name]["summary"]["median"]
            if base_med:
                print(f"{name} vs {baseline}: {(med - base_med) / base_med * 100:+.1f}% "
                      f"(median {med:.2f} vs {base_med:.2f})")

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        payload = {
            "label": args.label,
            "metric_regex": args.metric,
            "rounds": args.rounds,
            "warmup": args.warmup,
            "variants": results,
            "raw": raw,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
