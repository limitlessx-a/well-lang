import json
import multiprocessing as mp
import os
import socket
import statistics
import subprocess
import sys
import time

COMP = r"E:\Work\WellLangV2\project\compiler"
sys.path.insert(0, os.path.join(COMP, "tools", "perf"))
import bench_http as bh

REQ = b"GET /bench HTTP/1.1\r\nHost: bench\r\nConnection: keep-alive\r\n\r\n"
BUILD = os.path.join(COMP, "build", "perf")
GODIR = os.path.join(BUILD, "http_go.exe")
JAVA_OUT = os.path.join(BUILD, "java")


def measure(port: int, proc, clients=8, seconds=3.0, iterations=50):
    bh.wait_ready(port, proc)
    q = mp.Queue()
    p = mp.Process(target=bh.latency_worker, args=(port, iterations, REQ, q))
    p.start()
    samples = q.get(timeout=120)
    p.join(timeout=10)
    q2 = mp.Queue()
    procs = [mp.Process(target=bh.throughput_worker, args=(port, seconds, REQ, q2))
             for _ in range(clients)]
    for pr in procs:
        pr.start()
    total = errors = 0
    end = time.time() + seconds + 10
    for _ in procs:
        n, err, _detail = q2.get(timeout=max(1, end - time.time()))
        total += n
        errors += err
    for pr in procs:
        pr.join(timeout=5)
    return {
        "rps": round(total / seconds, 1),
        "errors": errors,
        "p50_us": bh.pct(samples, 0.50),
        "p99_us": bh.pct(samples, 0.99),
        "rss_mb": round(bh.RssSampler(proc.pid).read() / (1024 * 1024), 1),
    }


def run(label, argv, env=None, cwd=COMP):
    port = bh.free_port()
    e = dict(os.environ)
    if env:
        e.update(env)
    proc = subprocess.Popen(argv + [str(port)] if label not in ("well-tcc", "well-rel") else argv,
                            cwd=cwd, env=e, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        r = measure(port, proc)
        r["label"] = label
        return r
    finally:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def run_well(label, release):
    port = bh.free_port()
    c_path = bh.build_exe(bh.SERVER_TEMPLATE, port) if release else bh.build_c(bh.SERVER_TEMPLATE, port)
    proc = (bh.spawn_exe_server(c_path, 12) if release
            else bh.spawn_tcc_server(c_path, 12))
    try:
        r = measure(port, proc)
        r["label"] = label
        return r
    finally:
        proc.kill()
        proc.communicate(timeout=5)


def main():
    out = []
    out.append(run_well("well-tcc", False))
    out.append(run_well("well-rel", True))
    out.append(run("go", [GODIR], env={"GOMAXPROCS": "12"}))
    out.append(run("java", [r"D:\Soft\jdk21\bin\java.exe", "-Xms512m", "-Xmx512m",
                            "-cp", JAVA_OUT, "HttpBench"], env={}))
    out.append(run("erlang", [r"D:\Soft\erlang\bin\erl.exe", "-noshell", "-pa",
                              os.path.join(COMP, "build", "perf", "beam"),
                              "-s", "http_server", "main"], env={}))
    out.append(run("c", [os.path.join(COMP, "build", "perf", "http_c.exe")], env={}))
    out.append(run("cpp", [os.path.join(COMP, "build", "perf", "http_cpp.exe")], env={}))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
