import json
import multiprocessing as mp
import os
import sys
import time

COMP = r"E:\Work\WellLangV2\project\compiler"
sys.path.insert(0, os.path.join(COMP, "tools", "perf"))
import bench_http as bh
import bench_http_pipeline as bp

BUILD = os.path.join(COMP, "build", "perf")
JAVA_OUT = os.path.join(BUILD, "java")


def measure(port, proc, clients=8, seconds=3.0, depth=32):
    bh.wait_ready(port, proc)
    q = mp.Queue()
    procs = [mp.Process(target=bp.pipeline_worker, args=(port, seconds, depth, q))
             for _ in range(clients)]
    for p in procs:
        p.start()
    total = errors = 0
    deadline = time.time() + seconds + 15
    for _ in procs:
        n, e = q.get(timeout=max(1, deadline - time.time()))
        total += n
        errors += e
    for p in procs:
        p.join(timeout=5)
    return {"rps": round(total / seconds, 1), "errors": errors,
            "rss_mb": round(bh.RssSampler(proc.pid).read() / (1024 * 1024), 1)}


def run_well(label, release):
    port = bh.free_port()
    c = bh.build_exe(bh.SERVER_TEMPLATE, port) if release else bh.build_c(bh.SERVER_TEMPLATE, port)
    proc = bh.spawn_exe_server(c, 12) if release else bh.spawn_tcc_server(c, 12)
    try:
        r = measure(port, proc)
        r["label"] = label
        return r
    finally:
        proc.kill()
        proc.communicate(timeout=5)


def run(label, argv):
    port = bh.free_port()
    proc = __import__("subprocess").Popen(argv + [str(port)], cwd=COMP,
                                         stdout=__import__("subprocess").DEVNULL,
                                         stderr=__import__("subprocess").PIPE)
    try:
        r = measure(port, proc)
        r["label"] = label
        return r
    finally:
        proc.kill()
        proc.communicate(timeout=5)


def main():
    out = [run_well("well-tcc", False), run_well("well-rel", True)]
    out.append(run("go", [os.path.join(BUILD, "http_go.exe")]))
    out.append(run("java", [r"D:\Soft\jdk21\bin\java.exe", "-Xms512m", "-Xmx512m",
                            "-cp", JAVA_OUT, "HttpBench"]))
    out.append(run("c", [os.path.join(BUILD, "http_c.exe")]))
    out.append(run("cpp", [os.path.join(BUILD, "http_cpp.exe")]))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
