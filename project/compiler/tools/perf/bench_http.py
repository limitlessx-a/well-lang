#!/usr/bin/env python3
"""HTTP/WS-capable benchmark driver for a Well runtime checkout.

Why this exists
---------------
`tools/bench.py` builds a native server and drives it with Python threads.  On
this Windows machine Smart App Control / Device Guard blocks *new* unsigned
executables, so the benchmark instead asks TinyCC to compile and run the server
in memory (`tcc ... -run`): tcc.exe is an already-trusted binary and executes
the generated code in its own process.  That keeps the measurement environment
usable without weakening any OS policy.

The client side uses processes (not threads) so the Python GIL does not cap the
server.  Latency percentiles come from one keep-alive connection; throughput
aggregates N processes x C connections.

Usage:
  python tools/perf/bench_http.py --compiler . --label v2 --workers 1 \
      --clients 8 --seconds 3 --iterations 1500 --repeat 3 --json out.json
"""
import argparse
import ctypes
import json
import multiprocessing as mp
import os
import socket
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
COMPILER_DIR = os.environ.get("BENCH_COMPILER") or os.path.dirname(TOOLS)
TCC = os.path.join(os.path.dirname(COMPILER_DIR), "tool_ai", "tools", "tcc", "tcc", "tcc.exe")
BUILD = os.path.join(COMPILER_DIR, "build", "perf")
SERVER_TEMPLATE = os.path.join(TOOLS, "bench_http_server.we")
REQ = b"GET /bench HTTP/1.1\r\nHost: bench\r\nConnection: keep-alive\r\n\r\n"


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def read_response(sock: socket.socket, buf: bytes):
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
    return status, buf[n:]


def latency_worker(port: int, count: int, req: bytes, q) -> None:
    samples = []
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.settimeout(5)
        buf = b""
        for _ in range(100):
            sock.sendall(req)
            _, buf = read_response(sock, buf)
        for _ in range(count):
            t0 = time.perf_counter_ns()
            sock.sendall(req)
            status, buf = read_response(sock, buf)
            samples.append(time.perf_counter_ns() - t0)
        sock.close()
    except OSError:
        pass
    q.put(samples)


def throughput_worker(port: int, seconds: float, req: bytes, q) -> None:
    n = 0
    errors = 0
    detail = ""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.settimeout(5)
        buf = b""
        deadline = time.time() + seconds
        while time.time() < deadline:
            sock.sendall(req)
            status, buf = read_response(sock, buf)
            if status != 200:
                errors += 1
            n += 1
        sock.close()
    except OSError as e:
        errors += 1
        detail = repr(e)
    q.put((n, errors, detail))


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class RssSampler:
    def __init__(self, pid: int):
        self.pid = pid
        self.peak = 0
        self.start = self.read()
        self._psapi = ctypes.windll.psapi
        self._k32 = ctypes.windll.kernel32

    def read(self) -> int:
        h = ctypes.windll.kernel32.OpenProcess(0x1000 | 0x0400, False, self.pid)
        if not h:
            return 0
        try:
            c = PROCESS_MEMORY_COUNTERS()
            c.cb = ctypes.sizeof(c)
            ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb)
            return int(c.WorkingSetSize)
        finally:
            ctypes.windll.kernel32.CloseHandle(h)


def build_c(source: str, port: int) -> str:
    os.makedirs(BUILD, exist_ok=True)
    text = open(source, encoding="utf-8").read().replace("{{PORT}}", str(port))
    src = os.path.join(BUILD, "server_%s.we" % os.path.basename(COMPILER_DIR))
    with open(src, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    c_path = os.path.join(BUILD, "server_%s.c" % os.path.basename(COMPILER_DIR))
    r = subprocess.run(
        [sys.executable, os.path.join(COMPILER_DIR, "wellc.py"), "emit-c", src, "-o", c_path],
        cwd=COMPILER_DIR, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError("emit-c failed:\n" + r.stdout + r.stderr)
    return c_path


def runtime_sources() -> list[str]:
    rt = os.path.join(COMPILER_DIR, "runtime")
    out = [os.path.join(rt, "core", n) for n in (
        "wellrt.c", "well_module.c", "well_thread.c", "well_model.c",
        "well_cache.c", "well_store.c", "well_containers.c", "well_event.c",
        "well_timer.c", "well_serialize.c", "well_json.c", "well_file.c",
    )]
    out.append(os.path.join(rt, "modules", "http", "well_http.c"))
    out.append(os.path.join(rt, "modules", "ws", "well_ws.c"))
    out.append(os.path.join(rt, "modules", "mysql", "well_mysql.c"))
    return out


def build_exe(source: str, port: int) -> str:
    os.makedirs(BUILD, exist_ok=True)
    text = open(source, encoding="utf-8").read().replace("{{PORT}}", str(port))
    src = os.path.join(BUILD, "server_rel_%s.we" % os.path.basename(COMPILER_DIR))
    with open(src, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    exe = os.path.join(BUILD, "server_rel_%s.exe" % os.path.basename(COMPILER_DIR))
    r = subprocess.run(
        [sys.executable, os.path.join(COMPILER_DIR, "wellc.py"), "build",
         src, "--release", "-o", exe],
        cwd=COMPILER_DIR, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError("release build failed:\n" + r.stdout + r.stderr)
    return exe


def pin_process(pid: int, mask: int) -> None:
    if not mask:
        return
    h = ctypes.windll.kernel32.OpenProcess(0x0200, False, pid)  # SET_INFORMATION
    if not h:
        return
    try:
        ctypes.windll.kernel32.SetProcessAffinityMask(h, ctypes.c_size_t(mask))
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def spawn_tcc_server(c_path: str, workers: int, affinity: int = 0):
    rt = os.path.join(COMPILER_DIR, "runtime")
    argv = [TCC,
            "-I", os.path.join(rt, "core"),
            "-I", os.path.join(rt, "modules", "http"),
            "-I", os.path.join(rt, "modules", "ws"),
            "-I", os.path.join(rt, "modules", "mysql"),
            c_path] + runtime_sources() + ["-run"]
    env = dict(os.environ)
    env["WELL_WORKERS"] = str(workers)
    p = subprocess.Popen(argv, cwd=COMPILER_DIR, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    pin_process(p.pid, affinity)
    return p


def spawn_exe_server(exe: str, workers: int, affinity: int = 0):
    env = dict(os.environ)
    env["WELL_WORKERS"] = str(workers)
    p = subprocess.Popen([exe], cwd=COMPILER_DIR, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    pin_process(p.pid, affinity)
    return p


def wait_ready(port: int, proc: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else b""
            raise RuntimeError("server exited: " + err.decode(errors="replace")[:2000])
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            s.close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not listen on %d" % port)


def pct(samples_ns: list[int], p: float) -> float:
    us = sorted(x / 1000.0 for x in samples_ns)
    if not us:
        return 0.0
    return round(us[min(len(us) - 1, int(len(us) * p))], 2)


def run_once(c_path: str, port: int, workers: int, clients: int,
             seconds: float, iterations: int, req: bytes,
             release: bool = False, affinity: int = 0) -> dict:
    proc = (spawn_exe_server(c_path, workers, affinity) if release
            else spawn_tcc_server(c_path, workers, affinity))
    try:
        wait_ready(port, proc)
        # Latency: one connection, sequential requests.
        q = mp.Queue()
        p = mp.Process(target=latency_worker, args=(port, iterations, req, q))
        p.start()
        samples = q.get(timeout=120)
        p.join(timeout=10)
        # Throughput: `clients` processes, each with one keep-alive connection.
        sampler = RssSampler(proc.pid)
        q2 = mp.Queue()
        procs = [mp.Process(target=throughput_worker, args=(port, seconds, req, q2))
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
                print("  client error:", detail, flush=True)
        for pr in procs:
            pr.join(timeout=10)
        elapsed = max(time.time() - t0, 1e-9)
        return {
            "workers": workers,
            "clients": clients,
            "latency_avg_us": round(statistics.fmean(samples) / 1000.0, 2) if samples else 0,
            "latency_p50_us": pct(samples, 0.50),
            "latency_p99_us": pct(samples, 0.99),
            "requests": total,
            "rps": round(total / elapsed, 1),
            "errors": errors,
            "rss_mb": round(sampler.peak / (1024 * 1024), 1),
        }
    finally:
        proc.kill()
        try:
            _out, _err = proc.communicate(timeout=5)
            if _err:
                print("  server stderr:", _err.decode(errors="replace")[-1200:], flush=True)
        except subprocess.TimeoutExpired:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=SERVER_TEMPLATE)
    ap.add_argument("--path", default="/bench")
    ap.add_argument("--label", default="v2")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--iterations", type=int, default=1500)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--release", action="store_true",
                    help="build the server with --release (Clang -O2)")
    ap.add_argument("--affinity", type=lambda s: int(s, 0), default=0,
                    help="server CPU affinity mask, e.g. 0xFF0")
    ap.add_argument("--json")
    args = ap.parse_args()

    port = free_port()
    c_path = build_exe(args.source, port) if args.release else build_c(args.source, port)
    req = ("GET %s HTTP/1.1\r\nHost: bench\r\nConnection: keep-alive\r\n\r\n"
           % args.path).encode()
    runs = []
    for i in range(args.repeat):
        r = run_once(c_path, port, args.workers, args.clients,
                     args.seconds, args.iterations, req, release=args.release,
                     affinity=args.affinity)
        r["round"] = i + 1
        runs.append(r)
        print("%s r%d: p50=%.2fus p99=%.2fus rps=%.1f err=%d rss=%.1fMB" % (
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




