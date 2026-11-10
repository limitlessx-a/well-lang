#!/usr/bin/env python3
"""HTTP/1.1 pipelining throughput driver.

Sends `--depth` keep-alive requests in one write, then reads exactly that many
responses before the next batch.  This removes the per-request client round trip
from the measurement and shows the server-side ceiling of the existing
thread-per-connection model.
"""
import argparse
import multiprocessing as mp
import os
import socket
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bench_http import (  # noqa: E402
    COMPILER_DIR, SERVER_TEMPLATE, build_c, free_port, spawn_tcc_server, wait_ready, RssSampler,
)

REQ = b"GET /bench HTTP/1.1\r\nHost: bench\r\nConnection: keep-alive\r\n\r\n"


def pipeline_worker(port: int, seconds: float, depth: int, q) -> None:
    total = 0
    errors = 0
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(10)
        batch = REQ * depth
        buf = b""
        deadline = time.time() + seconds
        while time.time() < deadline:
            s.sendall(batch)
            got = 0
            while got < depth:
                while b"\r\n\r\n" not in buf:
                    chunk = s.recv(65536)
                    if not chunk:
                        raise ConnectionError("server closed")
                    buf += chunk
                head, rest = buf.split(b"\r\n\r\n", 1)
                clen = 0
                for line in head.split(b"\r\n")[1:]:
                    if line.lower().startswith(b"content-length:"):
                        clen = int(line.split(b":", 1)[1].strip())
                        break
                while len(rest) < clen:
                    chunk = s.recv(65536)
                    if not chunk:
                        raise ConnectionError("server closed in body")
                    rest += chunk
                buf = rest[clen:]
                got += 1
            total += depth
        s.close()
    except Exception:
        errors += 1
    q.put((total, errors))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--depth", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=2.0)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--workers", type=int, default=1)
    args = ap.parse_args()

    port = free_port()
    c_path = build_c(SERVER_TEMPLATE, port)
    proc = spawn_tcc_server(c_path, args.workers)
    runs = []
    try:
        wait_ready(port, proc)
        sampler = RssSampler(proc.pid)
        for _ in range(args.repeat):
            q = mp.Queue()
            procs = [mp.Process(target=pipeline_worker,
                                args=(port, args.seconds, args.depth, q))
                     for _ in range(args.clients)]
            t0 = time.time()
            for p in procs:
                p.start()
            total = errors = 0
            for _ in procs:
                n, e = q.get(timeout=args.seconds + 60)
                total += n
                errors += e
            for p in procs:
                p.join(timeout=10)
            elapsed = max(time.time() - t0, 1e-9)
            rss = sampler.read() / (1024 * 1024)
            runs.append({"rps": total / elapsed, "errors": errors, "rss": rss})
            print("depth %d: rps=%.1f errors=%d rss=%.1fMB" % (
                args.depth, total / elapsed, errors, rss))
    finally:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
    ok = [r for r in runs if r["errors"] == 0]
    print("median_rps %.1f" % (statistics.median(r["rps"] for r in ok) if ok else 0))
    return 0 if len(ok) == len(runs) else 1


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
