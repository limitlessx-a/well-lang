import os
import subprocess
import sys


def main():
    V3 = r"E:\Work\WellLangV3"
    V2B = r"E:\Work\WellLangV2\project\test\lang_bench"
    sys.path.insert(0, V2B)
    import http_compare as hc
    import bench_http as bh

    port = bh.free_port()
    src = os.path.join(V3, "project", "compiler", "build", "v3_bench_server.we")
    text = open(os.path.join(V3, "project", "compiler", "tools", "bench_http_server.we"),
                encoding="utf-8").read().replace("{{PORT}}", str(port))
    open(src, "w", encoding="utf-8", newline="\n").write(text)
    exe = os.path.join(r"D:\Soft\_downloads", "v3_bench_server_%d.exe" % port)
    env = dict(os.environ)
    env["WELL_CC"] = os.path.join(V3, "project", "tool_ai", "clang.cmd")
    env["WELL_CFLAGS"] = "-O2 -fwrapv"
    r = subprocess.run([sys.executable, os.path.join(V3, "project", "compiler", "wellc.py"),
                        "build", src, "-o", exe],
                       cwd=os.path.join(V3, "project", "compiler"),
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        raise SystemExit(1)
    env["WELL_WORKERS"] = "12"
    proc = subprocess.Popen([exe], cwd=os.path.join(V3, "project", "compiler"), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        print(hc.measure(port, proc))
    finally:
        proc.kill()
        proc.communicate(timeout=5)


if __name__ == "__main__":
    main()

