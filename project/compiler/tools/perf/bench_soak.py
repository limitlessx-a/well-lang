#!/usr/bin/env python3
"""RSS soak test: keep a Well server under steady load and report memory slope.

Usage:
  python tools/perf/bench_soak.py --source tools/bench_http_server.we \
      --path /bench --seconds 120 --clients 8 --interval 1

The server runs via `tcc ... -run` (same reason as bench_http.py).  The client
side reuses the process-based keep-alive workers from bench_http.
"""
import argparse
import json
import multiprocessing as mp
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bench_http import (  # noqa: E402
    COMPILER_DIR, BUILD, free_port, build_c, spawn_tcc_server, wait_ready,
    RssSampler, throughput_worker,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=os.path.join(os.path.dirname(HERE), "bench_http_server.we"))
    ap.add_argument("--path", default="/bench")
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--warmup", type=float, default=5.0)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--json")
    ap.add_argument("--progress", help="append each RSS sample as JSONL for live monitoring")
    args = ap.parse_args()

    port = free_port()
    c_path = build_c(args.source, port)
    req = ("GET %s HTTP/1.1\r\nHost: bench\r\nConnection: keep-alive\r\n\r\n"
           % args.path).encode()
    proc = spawn_tcc_server(c_path, 1)
    samples = []
    progress = open(args.progress, "w", encoding="utf-8", newline="\n") if args.progress else None
    try:
        wait_ready(port, proc)
        sampler = RssSampler(proc.pid)
        # Warmup: let pools/TLS caches reach steady state before sampling.
        q = mp.Queue()
        warm = [mp.Process(target=throughput_worker, args=(port, args.warmup, req, q))
                for _ in range(args.clients)]
        for p in warm:
            p.start()
        for _ in warm:
            q.get(timeout=max(30, args.warmup + 30))
        for p in warm:
            p.join(timeout=10)

        q2 = mp.Queue()
        procs = [mp.Process(target=throughput_worker, args=(port, args.seconds, req, q2))
                 for _ in range(args.clients)]
        for p in procs:
            p.start()
        t0 = time.time()
        while any(p.is_alive() for p in procs):
            sample = (round(time.time() - t0, 2), sampler.read())
            samples.append(sample)
            if progress:
                progress.write(json.dumps({"t": sample[0],
                                           "rss_mb": round(sample[1] / (1024 * 1024), 2)}) + "\n")
                progress.flush()
            time.sleep(args.interval)
        total = 0
        errors = 0
        for _ in procs:
            n, e, detail = q2.get(timeout=30)
            total += n
            errors += e
            if detail:
                print("client error:", detail)
        for p in procs:
            p.join(timeout=10)
        elapsed = max(time.time() - t0, 1e-9)
    finally:
        if progress:
            progress.close()
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass

    mb = [(t, b / (1024 * 1024)) for t, b in samples]
    if len(mb) >= 2:
        start = mb[0][1]
        end = mb[-1][1]
        peak = max(v for _, v in mb)
        # Startup allocates pools/caches; measure the slope after that step.
        steady = [(t, v) for t, v in mb if t >= min(5.0, max(1.0, elapsed * 0.1))]
        if len(steady) >= 2:
            n = len(steady)
            mx = sum(t for t, _ in steady) / n
            my = sum(v for _, v in steady) / n
            den = sum((t - mx) ** 2 for t, _ in steady)
            slope = (sum((t - mx) * (v - my) for t, v in steady) / den * 3600.0) if den else 0.0
            steady_start, steady_end = steady[0][1], steady[-1][1]
        else:
            slope = (end - start) / elapsed * 3600.0
            steady_start, steady_end = start, end
        print("elapsed %.1fs requests %d rps %.1f errors %d" % (
            elapsed, total, total / elapsed, errors))
        print("rss_start_mb %.2f rss_end_mb %.2f rss_peak_mb %.2f slope_mb_per_h %.2f" % (
            start, end, peak, slope))
        print("steady_rss_mb %.2f..%.2f steady_slope_mb_per_h %.2f" % (
            steady_start, steady_end, slope))
        print("samples " + " ".join("%.1f:%.2f" % s for s in mb[:5]) + " ...")
        out = {
            "seconds": elapsed,
            "requests": total,
            "rps": round(total / elapsed, 1),
            "errors": errors,
            "rss_start_mb": round(start, 2),
            "rss_end_mb": round(end, 2),
            "rss_peak_mb": round(peak, 2),
            "steady_slope_mb_per_h": round(slope, 2),
            "samples": [{"t": t, "rss_mb": round(v, 2)} for t, v in mb],
        }
        if args.json:
            os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
            with open(args.json, "w", encoding="utf-8", newline="\n") as f:
                json.dump(out, f, indent=2)
                f.write("\n")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
