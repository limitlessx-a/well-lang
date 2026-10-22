#!/usr/bin/env python3
"""Reproducible benchmarks for the Well runtime.

Scenarios:
    http_latency     sequential keep-alive GET /bench (int latency p50/p99)
    http_throughput  N keep-alive clients for S seconds (req/s)
    parser           lex+parse of the bench server source (compiler frontend)

Usage:
    python tools/bench.py --scenario all --json build/bench/result.json
    python tools/bench.py --scenario http --save-baseline build/bench/baseline.json
    python tools/bench.py --compare build/bench/baseline.json --threshold 30
"""
import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
COMPILER_DIR = os.path.dirname(HERE)
BUILD = os.path.join(COMPILER_DIR, "build", "bench")
WELLC = os.path.join(COMPILER_DIR, "wellc.py")
SERVER_TEMPLATE = os.path.join(HERE, "bench_http_server.we")
sys.path.insert(0, COMPILER_DIR)

REQ = b"GET /bench HTTP/1.1\r\nHost: bench\r\nConnection: keep-alive\r\n\r\n"


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def stats_us(samples_ns: list[int], key_prefix: str) -> dict:
    us = sorted(x / 1000.0 for x in samples_ns)
    return {
        "samples": len(us),
        f"{key_prefix}_avg_us": round(statistics.fmean(us), 2),
        f"{key_prefix}_p50_us": round(us[len(us) // 2], 2),
        f"{key_prefix}_p99_us": round(us[min(len(us) - 1, int(len(us) * 0.99))], 2),
    }


def build_server(port: int) -> str:
    os.makedirs(BUILD, exist_ok=True)
    src = os.path.join(BUILD, "http_server.we")
    with open(SERVER_TEMPLATE, encoding="utf-8") as f:
        text = f.read().replace("{{PORT}}", str(port))
    with open(src, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    exe = os.path.join(BUILD, "http_server.exe")
    r = subprocess.run(
        [sys.executable, WELLC, "build", src, "-o", exe],
        cwd=COMPILER_DIR, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"bench server build failed:\n{r.stdout}\n{r.stderr}")
    return exe


def wait_ready(port: int, proc: subprocess.Popen, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise RuntimeError(
                "bench server exited:\n"
                + out.decode(errors="replace") + err.decode(errors="replace")
            )
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            s.close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"bench server did not listen on port {port}")


def read_response(sock: socket.socket, buf: bytes) -> tuple[int, bytes, bytes]:
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise OSError("connection closed")
        buf += chunk
    head, buf = buf.split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(b":")
        headers[k.strip().lower()] = v.strip()
    n = int(headers.get(b"content-length", b"0"))
    while len(buf) < n:
        chunk = sock.recv(65536)
        if not chunk:
            raise OSError("connection closed")
        buf += chunk
    body, buf = buf[:n], buf[n:]
    return status, body, buf


def one_request(sock: socket.socket, buf: bytes) -> bytes:
    sock.sendall(REQ)
    status, body, buf = read_response(sock, buf)
    if status != 200:
        raise OSError(f"status {status}")
    return buf


def bench_latency(port: int, iterations: int) -> dict:
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.settimeout(5)
    buf = b""
    try:
        for _ in range(100):
            buf = one_request(sock, buf)
        samples = []
        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            buf = one_request(sock, buf)
            samples.append(time.perf_counter_ns() - t0)
    finally:
        sock.close()
    return stats_us(samples, "latency")


def bench_throughput(port: int, clients: int, seconds: float) -> dict:
    deadline = time.time() + seconds
    lock = threading.Lock()
    total = 0
    errors = 0

    def worker() -> None:
        nonlocal total, errors
        n = 0
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sock.settimeout(5)
            buf = b""
            try:
                while time.time() < deadline:
                    buf = one_request(sock, buf)
                    n += 1
            finally:
                sock.close()
        except OSError:
            with lock:
                errors += 1
        with lock:
            total += n

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(clients)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0
    return {
        "clients": clients,
        "seconds": round(elapsed, 3),
        "requests": total,
        "rps": round(total / elapsed, 1) if elapsed > 0 else 0.0,
        "errors": errors,
    }


def bench_parser(iterations: int) -> dict:
    from wellc.lexer import lex
    from wellc.parser import Parser

    with open(SERVER_TEMPLATE, encoding="utf-8") as f:
        src = f.read().replace("{{PORT}}", "18080")
    samples = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        toks = lex(src, "bench_http_server.we")
        Parser(toks, "bench_http_server.we").parse_program()
        samples.append(time.perf_counter_ns() - t0)
    return stats_us(samples, "parse")


def run_http(iterations: int, clients: int, seconds: float) -> dict:
    port = free_port()
    exe = build_server(port)
    proc = subprocess.Popen(
        [exe], cwd=COMPILER_DIR,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        wait_ready(port, proc)
        lat = bench_latency(port, iterations)
        thr = bench_throughput(port, clients, seconds)
        return {"http_latency": lat, "http_throughput": thr}
    finally:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def compare(base: dict, cur: dict, threshold: float) -> bool:
    checks = [
        ("http_latency.latency_p50_us", base, cur, +1, "lower"),
        ("http_throughput.rps", base, cur, -1, "higher"),
        ("parser.parse_avg_us", base, cur, +1, "lower"),
    ]
    ok = True
    for path, b, c, direction, label in checks:
        bv = get_path(b, path)
        cv = get_path(c, path)
        if bv is None or cv is None or bv == 0:
            continue
        delta = (cv - bv) / bv * 100.0 * direction
        regressed = delta > threshold
        verdict = "FAIL" if regressed else "PASS"
        if regressed:
            ok = False
        print(f"compare {path}: base={bv} cur={cv} ({label} is better) "
              f"delta={delta:+.1f}% threshold={threshold:.0f}% -> {verdict}")
    return ok


def get_path(d: dict, path: str):
    for part in path.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description="Well runtime benchmarks")
    ap.add_argument("--scenario", choices=["all", "http", "parser"], default="all")
    ap.add_argument("--iterations", type=int, default=1000, help="HTTP latency samples")
    ap.add_argument("--clients", type=int, default=4, help="throughput clients")
    ap.add_argument("--seconds", type=float, default=2.0, help="throughput duration")
    ap.add_argument("--parser-iterations", type=int, default=500)
    ap.add_argument("--json", help="write the result JSON here")
    ap.add_argument("--save-baseline", help="write the result JSON as a baseline")
    ap.add_argument("--compare", help="compare against this baseline JSON")
    ap.add_argument("--threshold", type=float, default=30.0,
                    help="allowed regression percentage (default 30)")
    args = ap.parse_args()

    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "scenarios": {},
    }
    if args.scenario in ("all", "http"):
        print("bench: http ...")
        result["scenarios"].update(run_http(args.iterations, args.clients, args.seconds))
    if args.scenario in ("all", "parser"):
        print("bench: parser ...")
        result["scenarios"]["parser"] = bench_parser(args.parser_iterations)

    print(json.dumps(result["scenarios"], indent=2))
    out = args.json or args.save_baseline
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(result, f, indent=2)
            f.write("\n")
        print(f"bench: wrote {out}")

    ok = True
    if args.compare:
        with open(args.compare, encoding="utf-8") as f:
            base = json.load(f)
        print()
        ok = compare(base.get("scenarios", {}), result.get("scenarios", {}), args.threshold)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
