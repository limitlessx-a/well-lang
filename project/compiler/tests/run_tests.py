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


class WsClient:
    """Buffered WebSocket client.

    The 101 response and the first frames can share one TCP segment, so the
    bytes after the handshake must be kept for the frame reader.
    """

    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        self.sock.settimeout(5)
        self.buf = b""

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def handshake(self, path: str = "/chat"):
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET " + path + " HTTP/1.1\r\nHost: test\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: " + key +
               "\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("handshake closed")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n")[0]:
            raise OSError("handshake failed")
        return self

    def recv_frame(self):
        h = self._read(2)
        op = h[0] & 0x0F
        n = h[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read(8))[0]
        mask = self._read(4) if (h[1] & 0x80) else None
        data = self._read(n) if n else b""
        if mask:
            data = bytes(x ^ mask[i % 4] for i, x in enumerate(data))
        return op, data

    def send_text(self, text: str):
        data = text.encode()
        mask = os.urandom(4)
        h = bytes([0x81, 0x80 | len(data)]) + mask
        self.sock.sendall(h + bytes(x ^ mask[i % 4] for i, x in enumerate(data)))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def run_ws_concurrent_case(name: str, d: str, exe: str) -> bool:
    """The listener must keep accepting while a connection is already open."""
    port = int(open(os.path.join(d, "ws_port.txt"), encoding="utf-8").read().strip())
    p = subprocess.Popen([exe], cwd=COMPILER_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    a = b = None
    latency = 0.0
    frames = []
    try:
        for _ in range(50):
            if p.poll() is not None:
                out, err = p.communicate()
                print(f"FAIL {name}: server exited early\n{out.decode(errors='replace')}\n{err.decode(errors='replace')}")
                return False
            try:
                a = WsClient(port).handshake()
                break
            except OSError:
                time.sleep(0.1)
        if a is None:
            p.kill()
            print(f"FAIL {name}: server did not listen on {port}")
            return False
        a.recv_frame()  # welcome
        started = time.time()
        b = WsClient(port).handshake()
        b.recv_frame()  # welcome for the second connection
        latency = time.time() - started
        a.send_text("hi")
        try:
            for _ in range(8):
                op, data = a.recv_frame()
                frames.append((op, data))
                if op == 0x8:
                    break
        except OSError:
            # Ws.stop may close the socket without a close frame; the echo is
            # what this case checks.
            pass
    except OSError as e:
        p.kill()
        p.communicate()
        print(f"FAIL {name}: WebSocket client error: {e}")
        return False
    finally:
        if a is not None:
            a.close()
        if b is not None:
            b.close()
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
    if "hi" not in texts:
        print(f"FAIL {name}: echo missing, got {texts}")
        return False
    if latency > 1.5:
        print(f"FAIL {name}: listener blocked (second connection took {latency:.2f}s)")
        return False
    print(f"PASS {name} (concurrent {latency:.2f}s)")
    return True


def run_ws_case(name: str, d: str, exe: str) -> bool:
    port = int(open(os.path.join(d, "ws_port.txt"), encoding="utf-8").read().strip())
    p = subprocess.Popen([exe], cwd=COMPILER_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    c = None
    frames = []
    try:
        for _ in range(50):
            if p.poll() is not None:
                out, err = p.communicate()
                print(f"FAIL {name}: server exited early\n{out.decode(errors='replace')}\n{err.decode(errors='replace')}")
                return False
            try:
                c = WsClient(port).handshake()
                break
            except OSError:
                time.sleep(0.1)
        if c is None:
            p.kill()
            print(f"FAIL {name}: server did not listen on {port}")
            return False
        c.send_text("hi")
        for _ in range(8):
            op, data = c.recv_frame()
            frames.append((op, data))
            if op == 0x8:
                break
    except OSError as e:
        p.kill()
        p.communicate()
        print(f"FAIL {name}: WebSocket client error: {e}")
        return False
    finally:
        if c is not None:
            c.close()
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


def http_recv_response(s) -> bytes:
    """Read exactly one HTTP response (Content-Length framed)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = s.recv(4096)
        if not d:
            raise OSError("eof before headers")
        buf += d
    head, rest = buf.split(b"\r\n\r\n", 1)
    cl = 0
    for line in head.split(b"\r\n"):
        low = line.lower()
        if low.startswith(b"content-length:"):
            cl = int(line.split(b":", 1)[1].strip())
    while len(rest) < cl:
        d = s.recv(4096)
        if not d:
            raise OSError("eof before body")
        rest += d
    return head + b"\r\n\r\n" + rest[:cl]


def run_http_keepalive_case(name: str, d: str, exe: str) -> bool:
    """Several requests on one connection; the server must keep it open."""
    port = int(open(os.path.join(d, "http_port.txt"), encoding="utf-8").read().strip())
    spec = open(os.path.join(d, "http_keepalive.txt"), "rb").read()
    requests = [r for r in spec.replace(b"\r\n", b"\n").split(b"===\n") if r.strip()]
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
    all_resp = b""
    try:
        s.settimeout(5)
        for req in requests:
            s.sendall(req.replace(b"\n", b"\r\n"))
            all_resp += http_recv_response(s)
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
        p.communicate()
        print(f"FAIL {name}: server did not stop")
        return False
    if p.returncode != 0:
        print(f"FAIL {name}: exit={p.returncode}\n{err.decode(errors='replace')}")
        return False
    resp = all_resp.decode("utf-8", errors="replace")
    expect_file = os.path.join(d, "http_expect.txt")
    want = norm(open(expect_file, encoding="utf-8").read())
    got = norm(resp)
    if any(line and line not in got for line in want.split("\n")):
        print(f"FAIL {name}: HTTP response mismatch\n--- want ---\n{want}\n--- got ---\n{got}")
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
        resp_bytes = http_recv_response(s)
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
    resp = resp_bytes.decode("utf-8", errors="replace")
    expect_file = os.path.join(d, "http_expect.txt")
    want = norm(open(expect_file, encoding="utf-8").read())
    got = norm(resp)
    if any(line and line not in got for line in want.split("\n")):
        print(f"FAIL {name}: HTTP response mismatch\n--- want ---\n{want}\n--- got ---\n{got}")
        return False
    print(f"PASS {name}")
    return True


def run_http_concurrent_case(name: str, d: str, exe: str) -> bool:
    """The listener must accept a second connection while the first is still
    waiting for its handler: the request/response pair is not serialized."""
    port = int(open(os.path.join(d, "http_port.txt"), encoding="utf-8").read().strip())
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
    s2 = None
    data = b""
    latency = 0.0
    try:
        s.settimeout(5)
        s.sendall(b"GET /slow HTTP/1.1\r\nHost: test\r\n\r\n")
        time.sleep(0.3)
        started = time.time()
        s2 = socket.create_connection(("127.0.0.1", port), timeout=2)
        s2.settimeout(3)
        s2.sendall(b"GET /ok HTTP/1.1\r\nHost: test\r\n\r\n")
        data = http_recv_response(s2)
        latency = time.time() - started
    except OSError as e:
        p.kill()
        p.communicate()
        print(f"FAIL {name}: HTTP client error: {e}")
        return False
    finally:
        if s2 is not None:
            s2.close()
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
    resp = data.decode("utf-8", errors="replace")
    if "200 OK" not in resp or "ok" not in resp:
        print(f"FAIL {name}: concurrent response mismatch\n{resp}")
        return False
    if latency > 1.5:
        print(f"FAIL {name}: listener blocked (second response took {latency:.2f}s)")
        return False
    print(f"PASS {name} (concurrent {latency:.2f}s)")
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
        if os.path.isfile(os.path.join(d, "ws_concurrent.txt")):
            return run_ws_concurrent_case(name, d, exe)
        return run_ws_case(name, d, exe)
    if os.path.isfile(os.path.join(d, "http_port.txt")):
        if os.path.isfile(os.path.join(d, "http_keepalive.txt")):
            return run_http_keepalive_case(name, d, exe)
        if os.path.isfile(os.path.join(d, "http_concurrent.txt")):
            return run_http_concurrent_case(name, d, exe)
        return run_http_case(name, d, exe)

    env = os.environ.copy()
    env["WELL_WORKERS"] = "1"  # deterministic tests; env.txt may override
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
    ef = os.path.join(d, "expect_exit.txt")
    want_exit = int(open(ef, encoding="utf-8").read().strip()) if os.path.isfile(ef) else None
    sf = os.path.join(d, "expect_stderr.txt")
    if os.path.isfile(sf):
        want = open(sf, encoding="utf-8").read().strip()
        if norm(want) not in norm(err):
            print(f"FAIL {name}: stderr mismatch\n  want: {want}\n  got: {err.strip()[:800]}")
            return False
        if want_exit is None and rr.returncode != 0:
            print(f"FAIL {name}: expected exit 0, got {rr.returncode}")
            return False
    if want_exit is not None and rr.returncode != want_exit:
        print(f"FAIL {name}: expected exit {want_exit}, got {rr.returncode}")
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
    from wellc.driver import find_cc, cc_flags, RUNTIME_DIR, RUNTIME_SOURCES
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
        ("cleanup_batch_test", "cleanup batch ok"),
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
               "-I", os.path.join(RUNTIME_DIR, "modules", "mysql")]
        cmd += cc_flags()
        cmd += ["-o", exe, src]
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
