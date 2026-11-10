#!/usr/bin/env python3
"""Regression check: pipelined route + 404 + route responses must stay in order.

Sends /bench, /nope, /bench in one write and expects statuses [200, 404, 200].
Run after changing the HTTP response batching in well_http.c.
"""
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bench_http import SERVER_TEMPLATE, build_c, free_port, spawn_tcc_server, wait_ready


def read_statuses(sock, count):
    req = b"GET %s HTTP/1.1\r\nHost: bench\r\nConnection: keep-alive\r\n\r\n"
    sock.sendall(req % b"/bench" + req % b"/nope" + req % b"/bench")
    buf = b""
    status = []
    while len(status) < count:
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        status.append(int(head.split(b"\r\n", 1)[0].split(b" ")[1]))
        clen = 0
        for line in head.split(b"\r\n")[1:]:
            if line.lower().startswith(b"content-length:"):
                clen = int(line.split(b":", 1)[1])
        while len(rest) < clen:
            rest += sock.recv(65536)
        buf = rest[clen:]
    return status


def main() -> int:
    port = free_port()
    c_path = build_c(SERVER_TEMPLATE, port)
    proc = spawn_tcc_server(c_path, 1)
    try:
        wait_ready(port, proc)
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        status = read_statuses(s, 3)
        s.close()
        print("statuses", status)
        if status != [200, 404, 200]:
            print("ORDER BUG")
            return 1
        print("OK")
        return 0
    finally:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
