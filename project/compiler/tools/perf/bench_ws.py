#!/usr/bin/env python3
"""WebSocket throughput/latency benchmark.

The server is the .we echo program in tools/perf/ws_echo_server.we, compiled by
TinyCC and executed in memory (`tcc ... -run`) for the same Device Guard reason
as bench_http.py.  Clients are Python processes using a minimal RFC 6455
implementation (masked text frames, echo).

Usage:
  python tools/perf/bench_ws.py --label v2 --workers 1 --clients 8 \
      --seconds 3 --iterations 1500 --repeat 3 --json out.json
"""
import argparse
import base64
import json
import multiprocessing as mp
import os
import socket
import statistics
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bench_http import (  # noqa: E402
    COMPILER_DIR, TCC, BUILD, free_port, spawn_tcc_server, spawn_exe_server,
    build_exe, wait_ready, RssSampler, pct,
)

SERVER = os.path.join(HERE, "ws_echo_server.we")
PATH = "/echo"


class WSClient:
    """Minimal buffered RFC 6455 client (handshake leftovers are kept)."""

    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(5)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: bench\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (PATH, key))
        self.sock.sendall(req.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("handshake closed")
            data += chunk
        head, self.buf = data.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise OSError("handshake failed: %r" % head[:200])

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_text(self, payload: bytes) -> None:
        key = os.urandom(4)
        n = len(payload)
        head = bytearray([0x81])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack("!H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack("!Q", n)
        head += key
        masked = bytes(b ^ key[i & 3] for i, b in enumerate(payload))
        self.sock.sendall(bytes(head) + masked)

    def recv_frame(self) -> tuple[int, bytes]:
        h = self._read(2)
        opcode = h[0] & 0x0F
        n = h[1] & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read(8))[0]
        masked = h[1] & 0x80
        key = self._read(4) if masked else b""
        data = self._read(n)
        if masked:
            data = bytes(b ^ key[i & 3] for i, b in enumerate(data))
        return opcode, data

    def close(self) -> None:
        self.sock.close()


def ws_handshake(port: int) -> WSClient:
    return WSClient(port)


def ws_latency_worker(port: int, count: int, payload: bytes, q) -> None:
    samples = []
    try:
        s = WSClient(port)
        s.recv_frame()  # "ready" sent by the open handler
        for _ in range(count):
            t0 = time.perf_counter_ns()
            s.send_text(payload)
            op, _ = s.recv_frame()
            samples.append(time.perf_counter_ns() - t0)
            if op == 8:
                break
        s.close()
    except OSError:
        pass
    q.put(samples)


def ws_throughput_worker(port: int, seconds: float, payload: bytes, q) -> None:
    n = 0
    errors = 0
    detail = ""
    try:
        s = WSClient(port)
        s.recv_frame()
        deadline = time.time() + seconds
        while time.time() < deadline:
            s.send_text(payload)
            op, data = s.recv_frame()
            if op == 8:
                errors += 1
                break
            if data != payload:
                errors += 1
            n += 1
        s.close()
    except OSError as e:
        errors += 1
        detail = repr(e)
    q.put((n, errors, detail))


def build_c(port: int) -> str:
    os.makedirs(BUILD, exist_ok=True)
    text = open(SERVER, encoding="utf-8").read().replace("{{PORT}}", str(port))
    src = os.path.join(BUILD, "ws_server.we")
    with open(src, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    c_path = os.path.join(BUILD, "ws_server.c")
    import subprocess
    r = subprocess.run(
        [sys.executable, os.path.join(COMPILER_DIR, "wellc.py"), "emit-c", src, "-o", c_path],
        cwd=COMPILER_DIR, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError("emit-c failed:\n" + r.stdout + r.stderr)
    return c_path


def run_once(c_path: str, port: int, workers: int, clients: int,
             seconds: float, iterations: int, payload: bytes,
             release: bool = False) -> dict:
    proc = spawn_exe_server(c_path, workers) if release else spawn_tcc_server(c_path, workers)
    try:
        wait_ready(port, proc)
        q = mp.Queue()
        p = mp.Process(target=ws_latency_worker, args=(port, iterations, payload, q))
        p.start()
        samples = q.get(timeout=120)
        p.join(timeout=10)
        sampler = RssSampler(proc.pid)
        q2 = mp.Queue()
        procs = [mp.Process(target=ws_throughput_worker,
                            args=(port, seconds, payload, q2))
                 for _ in range(clients)]
        t0 = time.time()
        for pr in procs:
            pr.start()
        total = 0
        errors = 0
        while any(pr.is_alive() for pr in procs):
            sampler.peak = max(sampler.peak, sampler.read())
            time.sleep(0.05)
        for _ in procs:
            n, e, detail = q2.get(timeout=30)
            total += n
            errors += e
            if detail:
                print("  ws client error:", detail, flush=True)
        for pr in procs:
            pr.join(timeout=10)
        elapsed = max(time.time() - t0, 1e-9)
        return {
            "workers": workers,
            "clients": clients,
            "msgs": total,
            "latency_avg_us": round(statistics.fmean(samples) / 1000.0, 2) if samples else 0,
            "latency_p50_us": pct(samples, 0.50),
            "latency_p99_us": pct(samples, 0.99),
            "rps": round(total / elapsed, 1),
            "errors": errors,
            "rss_mb": round(sampler.peak / (1024 * 1024), 1),
        }
    finally:
        proc.kill()
        try:
            _out, _err = proc.communicate(timeout=5)
            if _err:
                print("  server stderr:", _err.decode(errors="replace")[-800:], flush=True)
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="v2")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=2.0)
    ap.add_argument("--iterations", type=int, default=1000)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--payload", type=int, default=32)
    ap.add_argument("--release", action="store_true",
                    help="build the server with --release (GCC/Clang -O2)")
    ap.add_argument("--json")
    args = ap.parse_args()

    port = free_port()
    c_path = build_exe(SERVER, port) if args.release else build_c(port)
    payload = b"x" * args.payload
    runs = []
    for i in range(args.repeat):
        r = run_once(c_path, port, args.workers, args.clients,
                     args.seconds, args.iterations, payload,
                     release=args.release)
        r["round"] = i + 1
        runs.append(r)
        print("%s r%d: p50=%.2fus p99=%.2fus msg/s=%.1f err=%d rss=%.1fMB" % (
            args.label, i + 1, r["latency_p50_us"], r["latency_p99_us"],
            r["rps"], r["errors"], r["rss_mb"]), flush=True)
    ok = [r for r in runs if r["errors"] == 0]
    summary = {
        "label": args.label,
        "compiler": COMPILER_DIR,
        "workers": args.workers,
        "runs": runs,
        "median_rps": statistics.median(r["rps"] for r in ok) if ok else 0,
        "min_p50_us": min((r["latency_p50_us"] for r in ok), default=0),
        "min_p99_us": min((r["latency_p99_us"] for r in ok), default=0),
        "max_rss_mb": max((r["rss_mb"] for r in ok), default=0),
    }
    print(json.dumps({k: summary[k] for k in
                      ("label", "workers", "median_rps", "min_p50_us",
                       "min_p99_us", "max_rss_mb")}, indent=2))
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump(summary, f, indent=2)
            f.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
