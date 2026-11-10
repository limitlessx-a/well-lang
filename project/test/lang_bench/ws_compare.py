import json
import multiprocessing as mp
import os
import subprocess
import sys
import time

COMP = r"E:\Work\WellLangV2\project\compiler"
sys.path.insert(0, os.path.join(COMP, "tools", "perf"))
import bench_http as bh
import bench_ws as bw

BUILD = os.path.join(COMP, "build", "perf")
GO = os.path.join(BUILD, "ws_go.exe")
PAYLOAD = b"hello"


def measure(port, proc, clients=8, seconds=3.0, iterations=50):
    bh.wait_ready(port, proc)
    q = mp.Queue()
    p = mp.Process(target=bw.ws_latency_worker, args=(port, iterations, PAYLOAD, q))
    p.start()
    samples = q.get(timeout=120)
    p.join(timeout=10)
    q2 = mp.Queue()
    procs = [mp.Process(target=bw.ws_throughput_worker, args=(port, seconds, PAYLOAD, q2))
             for _ in range(clients)]
    for x in procs:
        x.start()
    total = errors = 0
    deadline = time.time() + seconds + 15
    for _ in procs:
        n, e, _detail = q2.get(timeout=max(1, deadline - time.time()))
        total += n
        errors += e
    for x in procs:
        x.join(timeout=5)
    return {"rps": round(total / seconds, 1), "errors": errors,
            "p50_us": bh.pct(samples, 0.50), "p99_us": bh.pct(samples, 0.99)}


def well(label, release=False):
    port = bh.free_port()
    c = bh.build_exe(bw.SERVER, port) if release else bw.build_c(port)
    proc = bh.spawn_exe_server(c, 12) if release else bh.spawn_tcc_server(c, 12)
    try:
        r = measure(port, proc)
        r["label"] = label
        r["rss_mb"] = round(bh.RssSampler(proc.pid).read() / (1024 * 1024), 1)
        return r
    finally:
        proc.kill()
        proc.communicate(timeout=5)


def external(label, argv):
    port = bh.free_port()
    proc = subprocess.Popen(argv + [str(port)], cwd=COMP,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        r = measure(port, proc)
        r["label"] = label
        r["rss_mb"] = round(bh.RssSampler(proc.pid).read() / (1024 * 1024), 1)
        return r
    finally:
        proc.kill()
        proc.communicate(timeout=5)


def main():
    out = [well("well-tcc"), well("well-rel", True), external("go", [GO])]
    out.append(external("java", [r"D:\Soft\jdk21\bin\java.exe", "-Xms512m", "-Xmx512m",
                                 "-cp", os.path.join(BUILD, "java"), "WsBench"]))
    out.append(external("erlang", [r"D:\Soft\erlang\bin\erl.exe", "-noshell", "-pa",
                                   os.path.join(BUILD, "beam"), "-s", "ws_server", "main"]))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
