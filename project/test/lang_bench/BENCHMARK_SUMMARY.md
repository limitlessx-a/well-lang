# Well v2 benchmark summary

All numbers are same-machine, Windows 11 / i5-12400F, unless noted.
`TCC` = default bundled TinyCC; `Release` = `wellc --release` with Zig/Clang
`-O2 -fwrapv -DWELL_NO_OID_CHECK=1`. Each table is a same-round comparison;
HTTP numbers are sensitive to CPU frequency/affinity.

## CPU / containers / memory

| Workload | Well v2 | Java 21 | Notes |
|---|---:|---:|---|
| Array access (TCC) | 2.65 ns | 0.385 ns | |
| Array access (Release) | **0.35 ns** | 0.385 ns | |
| Sum loop (TCC) | 0.80 ns | — | v1 baseline 72.16 ns |
| Object new+release (TCC) | 60 ns | 43 ns | |
| Object new+release (Release) | **37–40 ns** | 43 ns | |
| Scalar struct escaping heap (TCC) | 35.5 ns | 43 ns | |
| Scalar struct stack | **3.5 ns** | 17.1 ns | non-escaping |
| Heap Array create+push+release (TCC) | 45.5 ns | 25.3 ns | |
| Heap Array (Release) | **17–20 ns** | 25.3 ns | |
| Heap Map create+set+get+release (TCC) | 65 ns | 49 ns | |
| Heap Map (Release) | **29–31 ns** | 49 ns | |
| 8B String new+release (Release) | **16–17 ns** | — | TCC 49 ns |
| `String.fromInt` (Release) | **18–19 ns** | — | TCC 54 ns |

Non-escaping (stack/region) paths:

| Workload | Well v2 | Java/Go/C++ |
|---|---:|---|
| Scalar struct | 3.5 ns | 17.1 / 16.7 / 25.8 |
| Small Array | 7.5 ns | 19.2 (Java, escaped) |
| Array + 2 pushes | ~16 ns | 25.0 / 12 / 94 |
| String Array | 10.5 ns | 22.4 (Java) |
| Map literal + 2 writes | ~10 ns | 60 / 90 / 135 |
| Escaping array push | ~100 ns | — |
| Escaping String array push | ~111 ns | — |
| Escaping Map constant writes | ~130 ns | — |
| Escaping String Map writes | ~145 ns | — |
| Fresh ref array push | ~195 ns | — |
| Fresh ref Map writes | ~254 ns | — |

## JSON (150B object, 300k iterations)

| Build | parse | stringify |
|---|---:|---:|
| TCC | 1526 ns | 510 ns |
| **Release** | **583 ns** | **280 ns** |
| Go `encoding/json` | 1129 ns | 526 ns |

## HTTP keep-alive (8 clients, 3s, CPU-pinned server)

| Implementation | req/s | p50 | RSS |
|---|---:|---:|---:|
| C (Clang -O2) | 248–253k | ~21–25 µs | 4.4–5.0 MB |
| C++ (same source) | ~253k | ~25 µs | 5.0 MB |
| **Well Release** | **210–216k** | **~20.5–23 µs** | **4.8 MB** |
| Well TCC | 205–210k | ~21.6–22 µs | 13.8 MB |
| Go `net/http` | 84–156k | — | 17 MB |
| Java `HttpServer` | 60–98k | — | ~395 MB |
| Erlang `gen_tcp` | 56–81k | — | 53 MB |

## HTTP pipelining (depth 32)

| Implementation | req/s | RSS |
|---|---:|---:|
| C / C++ | ~1,248k | 4.3 MB |
| **Well Release** | **1,150k** | 4.8 MB |
| Well TCC | 664–803k | 13.7 MB |
| Go / Java | 27–281k / 60–102k | — |

## WebSocket echo (8 clients, 3s)

| Implementation | msg/s | p50 | p99 | RSS |
|---|---:|---:|---:|---:|
| Java hand-written RFC6455 | 238.9k | 61.9 µs | 185.9 µs | 92 MB |
| **Well Release** | 232.2k | **21.1 µs** | **41.1 µs** | **4.8 MB** |
| Well TCC | 226.0k | 21.4 µs | 48.9 µs | 13.7 MB |
| Go hand-written | 193.8k | 29.5 µs | 257.8 µs | 13.5 MB |
| Erlang `gen_tcp` | 59.3k | 57.2 µs | 188.4 µs | 55.8 MB |

## Reproduce

```powershell
# CPU/container microbench
python project\compiler\tools\perf\bench_runtime.py

# HTTP (pinned)
python project\compiler\tools\perf\bench_http.py --label v2 --workers 12 --clients 8 --affinity 0xFF0 --repeat 3

# HTTP release
python project\compiler\tools\perf\bench_http.py --label rel --workers 12 --clients 8 --affinity 0xFF0 --release

# HTTP pipelining
python project\test\lang_bench\http_pipeline_compare.py

# WebSocket
python project\test\lang_bench\ws_compare.py

# JSON
python project\compiler\wellc.py run project\compiler\tools\perf\bench_json.we --release
```

## Caveats

- Stack/region numbers compare non-escaping Well against escaped Java/Go objects;
  they show the cost of the escape, not a like-for-like allocation.
- HTTP/WS competitors are minimal hand-written or standard-library servers, not
  tuned frameworks (fasthttp/Netty/etc.).
- Java `HttpServer` and Go `net/http` have different concurrency models; protocol
  and payload are identical but implementation quality differs.
- HTTP throughput varies with CPU frequency/affinity; use same-round A/B.
- Release skips oid/frozen checks; TCC keeps them for strict tests.

## Final verification (round 49)

- `.we` **189/189**, C tests passed.
- HTTP keep-alive, server pinned to CPU 4–11, 12 workers / 8 clients / 3s:
  - TCC: **209.0k req/s**, p50 21.8 µs, p99 23.2 µs, RSS 14.3 MB
  - Release: **216.4k req/s**, p50 21.2 µs, p99 25.0 µs, RSS **5.6 MB**
- WebSocket echo: Well TCC 227.0k, **Well Release 231.0k** (p50 21.1 µs),
  Java 240.3k (p50 43.2), Go 194.0k, Erlang 59.7k.
- JSON Release: parse **570 ns**, stringify **216 ns**.
- Release runtime primitives:
  - object new+release **36.8 ns**
  - object alloc/free only **19.6 ns**
  - 8B String new+release **16.3 ns**
  - `String.fromInt` **17.9 ns**
  - `IdUtil.getId` **17.1 ns**
  - Array new+push+release **15.9 ns**

The Release build meets/exceeds Java on the CPU/container/memory workloads;
HTTP keep-alive is ~15% behind a minimal C/C++ server, pipelining ~8%.

