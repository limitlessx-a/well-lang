"""End-to-end tests for the Well MVP compiler.

Each case directory contains:
  main.we                  source
  expected.txt             expected stdout (optional)
  expect_exit.txt          expected exit code (optional, default 0)
  expect_fail.txt          substring expected in stderr, non-zero exit (runtime failure)
  expect_compile_error.txt substring expected from wellc on compile failure
"""
import base64
import os
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
COMPILER_DIR = os.path.dirname(HERE)
WELLC = os.path.join(COMPILER_DIR, "wellc.py")
BUILD = os.path.join(COMPILER_DIR, "build", "tests")
CASES = os.path.join(HERE, "cases")


def norm(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").strip().split("\n"))


def ws_recv_frame(s):
    h = s.recv(2)
    if len(h) < 2:
        raise OSError("short frame")
    op = h[0] & 0x0F
    n = h[1] & 0x7F
    if n == 126:
        n = struct.unpack(">H", s.recv(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", s.recv(8))[0]
    mask = s.recv(4) if (h[1] & 0x80) else None
    data = b""
    while len(data) < n:
        data += s.recv(n - len(data))
    if mask:
        data = bytes(x ^ mask[i % 4] for i, x in enumerate(data))
    return op, data


def ws_send_text(s, text):
    data = text.encode()
    mask = os.urandom(4)
    h = bytes([0x81, 0x80 | len(data)]) + mask
    s.sendall(h + bytes(x ^ mask[i % 4] for i, x in enumerate(data)))


def run_ws_case(name: str, d: str, exe: str) -> bool:
    port = int(open(os.path.join(d, "ws_port.txt"), encoding="utf-8").read().strip())
    p = subprocess.Popen([exe], cwd=COMPILER_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    s = None
    for _ in range(50):
        if p.poll() is not None:
            out, err = p.communicate()
            print(f"FAIL {name}: server exited early\n{out.decode(errors='replace')}\n{err.decode(errors='replace')}")
            return False
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            break
        except OSError:
            time.sleep(0.1)
    if s is None:
        p.kill()
        print(f"FAIL {name}: server did not listen on {port}")
        return False
    try:
        s.settimeout(5)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET /chat HTTP/1.1\r\nHost: test\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: " + key +
               "\r\nSec-WebSocket-Version: 13\r\n\r\n")
        s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += s.recv(4096)
        if b"101" not in resp.split(b"\r\n")[0]:
            raise OSError("handshake failed")
        ws_send_text(s, "hi")
        frames = []
        for _ in range(8):
            op, data = ws_recv_frame(s)
            frames.append((op, data))
            if op == 0x8:
                break
    except OSError as e:
        p.kill()
        p.communicate()
        print(f"FAIL {name}: WebSocket client error: {e}")
        return False
    finally:
        s.close()
    try:
        out, err = p.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        print(f"FAIL {name}: server did not stop")
        return False
    if p.returncode != 0:
        print(f"FAIL {name}: exit={p.returncode}\n{err.decode(errors='replace')}")
        return False
    texts = [x[1].decode("utf-8", errors="replace") for x in frames if x[0] == 1]
    close_code = 0
    for op, data in frames:
        if op == 0x8 and len(data) >= 2:
            close_code = (data[0] << 8) | data[1]
    got = "\n".join(["101"] + texts + [str(close_code)])
    want = norm(open(os.path.join(d, "ws_expect.txt"), encoding="utf-8").read())
    if any(line and line not in got for line in want.split("\n")):
        print(f"FAIL {name}: WebSocket response mismatch\n--- want ---\n{want}\n--- got ---\n{got}")
        return False
    print(f"PASS {name}")
    return True


def run_http_case(name: str, d: str, exe: str) -> bool:
    port_file = os.path.join(d, "http_port.txt")
    port = int(open(port_file, encoding="utf-8").read().strip())
    p = subprocess.Popen([exe], cwd=COMPILER_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    s = None
    for _ in range(50):
        if p.poll() is not None:
            out, err = p.communicate()
            print(f"FAIL {name}: server exited early\n{out.decode(errors='replace')}\n{err.decode(errors='replace')}")
            return False
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            break
        except OSError:
            time.sleep(0.1)
    if s is None:
        p.kill()
        print(f"FAIL {name}: server did not listen on {port}")
        return False
    try:
        s.settimeout(5)
        req_file = os.path.join(d, "http_request.txt")
        if os.path.isfile(req_file):
            request = open(req_file, "rb").read()
            request = request.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        else:
            request = b"GET /hello HTTP/1.1\r\nHost: test\r\n\r\n"
        s.sendall(request)
        chunks = []
        while True:
            data = s.recv(4096)
            if not data:
                break
            chunks.append(data)
    except OSError as e:
        p.kill()
        p.communicate()
        print(f"FAIL {name}: HTTP client error: {e}")
        return False
    finally:
        s.close()
    try:
        out, err = p.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        print(f"FAIL {name}: server did not stop")
        return False
    if p.returncode != 0:
        print(f"FAIL {name}: exit={p.returncode}\n{err.decode(errors='replace')}")
        return False
    resp = b"".join(chunks).decode("utf-8", errors="replace")
    expect_file = os.path.join(d, "http_expect.txt")
    want = norm(open(expect_file, encoding="utf-8").read())
    got = norm(resp)
    if any(line and line not in got for line in want.split("\n")):
        print(f"FAIL {name}: HTTP response mismatch\n--- want ---\n{want}\n--- got ---\n{got}")
        return False
    print(f"PASS {name}")
    return True


def run_case(name: str) -> bool:
    d = os.path.join(CASES, name)
    src = os.path.join(d, "main.we")
    exe = os.path.join(BUILD, name + ".exe")
    os.makedirs(BUILD, exist_ok=True)

    r = subprocess.run(
        [sys.executable, WELLC, "build", src, "-o", exe],
        cwd=COMPILER_DIR, capture_output=True, text=True,
    )
    ce = os.path.join(d, "expect_compile_error.txt")
    if os.path.isfile(ce):
        want = open(ce, encoding="utf-8").read().strip()
        if r.returncode == 0:
            print(f"FAIL {name}: expected compile error, but build succeeded")
            return False
        if want not in (r.stdout + r.stderr):
            print(f"FAIL {name}: compile error mismatch\n  want: {want}\n  got: {r.stderr.strip()[:500]}")
            return False
        print(f"PASS {name} (compile error)")
        return True
    if r.returncode != 0:
        print(f"FAIL {name}: build failed\n{r.stdout}\n{r.stderr}")
        return False

    if os.path.isfile(os.path.join(d, "ws_port.txt")):
        return run_ws_case(name, d, exe)
    if os.path.isfile(os.path.join(d, "http_port.txt")):
        return run_http_case(name, d, exe)

    env = os.environ.copy()
    if os.path.isfile(os.path.join(d, "rc_debug.txt")):
        env["WELL_RC_DEBUG"] = "1"
    envf = os.path.join(d, "env.txt")
    if os.path.isfile(envf):
        for line in open(envf, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    rr = subprocess.run([exe], cwd=COMPILER_DIR, capture_output=True, env=env)
    out = rr.stdout.decode("utf-8", errors="replace")
    err = rr.stderr.decode("utf-8", errors="replace")
    rcf = os.path.join(d, "rc_debug.txt")
    if os.path.isfile(rcf):
        want = open(rcf, encoding="utf-8").read().strip()
        if want not in err:
            print(f"FAIL {name}: rc debug mismatch\n  want: {want}\n  got: {err.strip()[:500]}")
            return False
    sf = os.path.join(d, "expect_stderr.txt")
    if os.path.isfile(sf):
        want = open(sf, encoding="utf-8").read().strip()
        if norm(want) not in norm(err):
            print(f"FAIL {name}: stderr mismatch\n  want: {want}\n  got: {err.strip()[:800]}")
            return False
        if rr.returncode != 0:
            print(f"FAIL {name}: expected exit 0, got {rr.returncode}")
            return False
    pf = os.path.join(d, "expect_panic.txt")
    if os.path.isfile(pf):
        want = open(pf, encoding="utf-8").read().strip()
        if want not in (out + err):
            print(f"FAIL {name}: panic mismatch\n  want: {want}\n  got: {err.strip()[:500]}")
            return False
        if rr.returncode != 0:
            print(f"FAIL {name}: expected isolated panic (exit 0), got exit {rr.returncode}")
            return False
        exp = os.path.join(d, "expected.txt")
        if os.path.isfile(exp):
            want_out = norm(open(exp, encoding="utf-8").read())
            got = norm(out)
            if want_out != got:
                print(f"FAIL {name}: stdout mismatch\n--- want ---\n{want_out}\n--- got ---\n{got}")
                return False
        print(f"PASS {name} (panic isolated)")
        return True
    ef = os.path.join(d, "expect_fail.txt")
    if os.path.isfile(ef):
        want = open(ef, encoding="utf-8").read().strip()
        if rr.returncode == 0:
            print(f"FAIL {name}: expected runtime failure, but exit=0")
            return False
        if want not in (out + err):
            print(f"FAIL {name}: runtime failure mismatch\n  want: {want}\n  got: {err.strip()[:500]}")
            return False
        print(f"PASS {name} (runtime failure)")
        return True

    ec = os.path.join(d, "expect_exit.txt")
    if os.path.isfile(ec):
        want_exit = int(open(ec, encoding="utf-8").read().strip())
        if rr.returncode != want_exit:
            print(f"FAIL {name}: exit {rr.returncode}, want {want_exit}")
            return False

    exp = os.path.join(d, "expected.txt")
    if os.path.isfile(exp):
        want = norm(open(exp, encoding="utf-8").read())
        got = norm(out)
        if want != got:
            print(f"FAIL {name}: stdout mismatch\n--- want ---\n{want}\n--- got ---\n{got}")
            return False
    print(f"PASS {name}")
    return True


def run_c_tests() -> bool:
    sys.path.insert(0, COMPILER_DIR)
    from wellc.driver import find_cc, RUNTIME_DIR, RUNTIME_SOURCES
    tests = [
        ("serialize_test", "serialize round-trip ok"),
        ("store_test", "model/store facade ok"),
        ("cleanup_test", "inactive oid cleanup ok"),
        ("mysql_conn_test", "mysql conn handle ok"),
        ("force_cleanup_test", "force cleanup ok"),
        ("ttl_event_pin_test", "ttl event pin ok"),
        ("pool_test", "pool reuse ok"),
        ("store_reload_test", "store reload ok"),
        ("cleanup_race_test", "cleanup race ok"),
        ("store_pure_test", "pure memory store ok"),
        ("work_stealing_test", "work stealing ok"),
    ]
    ok = True
    os.makedirs(BUILD, exist_ok=True)
    for name, expect in tests:
        src = os.path.join(HERE, "c", name + ".c")
        exe = os.path.join(BUILD, name + ".exe")
        cmd = [find_cc(), "-I", os.path.join(RUNTIME_DIR, "core"),
               "-I", os.path.join(RUNTIME_DIR, "modules", "http"),
               "-I", os.path.join(RUNTIME_DIR, "modules", "ws"),
               "-I", os.path.join(RUNTIME_DIR, "modules", "mysql"),
               "-o", exe, src]
        if os.name != "nt":
            cmd.append("-pthread")
        cmd += [os.path.join(RUNTIME_DIR, n) for n in RUNTIME_SOURCES]
        r = subprocess.run(cmd, cwd=COMPILER_DIR, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"FAIL C {name}: build\n{r.stdout}\n{r.stderr}")
            ok = False
            continue
        rr = subprocess.run([exe], cwd=COMPILER_DIR, capture_output=True, text=True)
        out = rr.stdout.strip()
        if rr.returncode != 0 or expect not in out:
            print(f"FAIL C {name}: run exit={rr.returncode}\n{out}\n{rr.stderr}")
            ok = False
            continue
        print(f"PASS C {name}")
    return ok


def main() -> int:
    names = sorted(n for n in os.listdir(CASES) if os.path.isdir(os.path.join(CASES, n)))
    ok = 0
    for n in names:
        if run_case(n):
            ok += 1
    c_ok = run_c_tests()
    print(f"\n{ok}/{len(names)} cases passed" + (", C tests passed" if c_ok else ""))
    return 0 if (ok == len(names) and c_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
