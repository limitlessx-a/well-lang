# Well v1 / v2 与 C/C++/Java/Go/Erlang 横向对比

日期：2026-10-24  
机器：Intel i5-12400F（6P/12T），Windows 11 26200，同机、同轮运行。

> **v3 已加入。** v3 位于 `E:\Work\WellLangV3`，是基于 v1 的性能优化工作区；
> 本次用 v3 推荐的 Clang `-O2 -fwrapv -flto` 构建。v3 不带自己的 TCC，
> 因此 v3 只列优化构建。v3 数据已按 5 个独立进程（HTTP/WS 为 3 个探针）
> 复测并取中位数。

构建：

- Well v1：v1 自带 TCC，无 Release profile；
- Well v3：v3 `clang.cmd`（Zig/Clang），`-O2 -fwrapv -flto`；
- Well v2 TCC：默认 TCC；
- Well v2 Release：`wellc --release`，Zig/Clang `-O2 -fwrapv -DWELL_NO_OID_CHECK=1`；
- C/C++：Zig/Clang `-O2`；
- Java：Temurin 21.0.12.1（JIT）；
- Go：1.27.3；
- Erlang：OTP 29.1.2（BEAM JIT）。

## 1. CPU：1000 万元素数组求和（2000 万次）

负载：`acc += a[i & 1023]`，1024 个 long，2000 万次。

| 实现 | ns/iter | 相对 C |
|---|---:|---:|
| C（Clang -O2） | **0.200** | 1.00× |
| Go 1.27 | 0.257 | 1.29× |
| C++（Clang -O2） | 0.261 | 1.31× |
| Java 21 | 0.380 | 1.90× |
| **Well v2 Release** | **~0.40** | **~2.0×** |
| Erlang OTP 29 | 2.98 | 14.9× |
| Well v2 TCC | 2.7 | 13.5× |
| **Well v3 (LTO)** | **5.6** | **28.0×** |
| Well v1 | 75.1 | 375× |

- v2 Release 已接近 Java/C++，约为 C 的 2×；
- v3 LTO 约 v1 的 **13.4×**，但仍比 v2 TCC 慢约 **2.1×**、比 v2 Release 慢约 **14×**；
- v2 TCC 约 v1 的 **28×**；
- v1 是旧 runtime/codegen，数组访问仍慢两个数量级。

## 2. CPU：纯求和循环（1 亿次）

负载：`for (i = 0; i < 100000000; i++) acc += i`。

| 实现 | ms | ns/iter | 说明 |
|---|---:|---:|---|
| C / C++ Clang -O2 | ~0 | ~0 | 被优化成闭式，**无有效数据** |
| Well v2 Release | ~0 | ~0 | 同样被优化掉 |
| Go 1.27 | 24.8 | 0.248 | |
| Java 21 | 26.1 | 0.261 | |
| **Well v2 TCC** | **84** | **0.84** | |
| Erlang OTP 29 | 134 | 1.34 | |
| Well v3 LTO | ~0 | ~0 | 同样被优化掉 |
| Well v1 | 7,000 | 70.0 | |

- 该负载对优化编译器不公平，主基准仍看第 1 节数组访问；
- v2 TCC 比 v1 快 **~83×**；
- v2 TCC 比 Go/Java 慢约 **3.2×**，但已远好于 v1。

## 3. 短生命周期分配（200 万次，语义不完全相同）

| 实现 | 操作 | ns/op |
|---|---|---:|
| Go 1.27 | `make([]byte,64)` 并保留 | **23.8** |
| C（Clang -O2） | `malloc(64)+free` | 25.5 |
| C++（Clang -O2） | `new char[64]+delete[]` | 27.1 |
| Java 21 | `new byte[64]` 并保留 | 30.8 |
| **Well v2 Release** | 对象 new+release，逃逸到字段 | **39.0** |
| **Well v3 LTO** | 同语义，Clang LTO | **60.0** |
| Well v2 TCC | 同语义 | 100.5 |
| Well v1 | 同语义 | 223.5 |
| Erlang OTP 29 | 64B `binary:copy` | 362.6 |

- Well 的对象路径包含 RC +（v1/TCC 下）registry/oid 检查，C/Go/Java 是普通堆分配；
- v2 Release 约为 Java 的 **1.27×**、C 的 **1.53×**；
- v3 比 v1 快约 **3.7×**，但仍比 v2 Release 慢约 **1.55×**；
- v2 Release 约为 v1 的 **5.7×** 快。

## 4. 容器创建（200 万次，Array/List + 一个元素）

| 实现 | 操作 | ns/op |
|---|---|---:|
| Erlang OTP 29 | `[I]` list | **5.9** |
| Go 1.27 | slice + append | 7.5 |
| Java 21 | `ArrayList` + add | 23.0 |
| **Well v2 Release** | `Array<int>` + push + 字段逃逸 | **30.0** |
| C++（Clang -O2） | `new vector` + push_back | 59.2 |
| Well v2 TCC | 同语义 | 87.0 |
| **Well v3 LTO** | 同语义，Clang LTO | **~119** |
| Well v1 | 同语义 | 284.5 |

- v2 Release 比 Java 慢约 **1.3×**，比 C++ vector 快；
- v2 Release 约为 v1 的 **9.5×**；v3 比 v1 快约 **2.4×**，但比 v2 TCC 慢；
- 语言容器语义不同（RC/oid/pool vs GC/轻量 list），只看数量级。

## 5. HTTP keep-alive（8 clients，3s，小响应 `ok`）

同一轮 v2/C/C++/Go/Java/Erlang 对比；v1 为单独一轮旧 server 测量。

| 实现 | req/s | p50 | RSS | 备注 |
|---|---:|---:|---:|---|
| C（Clang -O2） | **258.1k** | 20.6 µs | 4.3 MB | 手写 thread-per-conn |
| C++（Clang -O2） | 257.3k | 19.1 µs | 4.3 MB | 同源 |
| **Well v2 Release** | **238.3k** | 21.7 µs | **4.8 MB** | |
| Well v2 TCC | 223.2k | 21.4 µs | 13.7 MB | |
| **Well v3 LTO** | **160.6k** | 28.8 µs | 9.4 MB | Clang LTO |
| Go 1.27 `net/http` | 158.8k | 33.7 µs | 17.6 MB | 标准库 |
| Well v1 | 150.4k | 30.2 µs | 9.7 MB | 单独一轮，旧 server |
| Java 21 `HttpServer` | 99.9k | 286.7 µs | 394 MB | 固定线程池 |
| Erlang `gen_tcp` | 89.1k | 41.7 µs | 53.3 MB | 每连接一进程 |

- v2 Release 约为 C/C++ 的 **92%**，差距约 **8%**；
- v2 Release 约为 Java 的 **2.4×**、Go 的 **1.5×**、v1 的 **1.58×**、v3 的 **1.48×**；
- v2 RSS 与 C/C++ 同级，Java 约 394 MB；
- v3 HTTP 与 Go/旧 v1 同档，明显低于 v2。

## 6. WebSocket echo（8 clients，3s）

| 实现 | msg/s | p50 | p99 | RSS |
|---|---:|---:|---:|---:|
| Java 21 手写 RFC6455 | **244.5k** | 41.5 µs | 94.3 µs | 95.4 MB |
| **Well v2 Release** | 230.9k | **20.7 µs** | 73.9 µs | **4.9 MB** |
| Well v2 TCC | 230.1k | 20.8 µs | 39.8 µs | 13.7 MB |
| Go 1.27 手写 RFC6455 | 195.1k | 19.8 µs | 236.6 µs | 13.0 MB |
| **Well v3 LTO** | **145.8k** | 31.4 µs | 73.1 µs | n/a |
| Erlang OTP 29 `gen_tcp` | 61.2k | 42.3 µs | 228.0 µs | 55.1 MB |
| Well v1 | 未测 | — | — | — |

- Java 吞吐略高约 6%，但 p50 约 Well 的 2×、RSS 约 19×；
- Well v2 p50 最低，p99 接近 Go；v3 WS 明显低于 v2；
- v1 WS 尚未接入同一 harness，不能编造数据。

## 7. JSON（150B 对象，30 万次）

| 实现 | parse | stringify |
|---|---:|---:|
| **Well v2 Release** | **570 ns** | **216 ns** |
| **Well v3 LTO** | **1166 ns** | **463 ns** |
| Go 1.27 `encoding/json` | 1129 ns | 526 ns |
| Well v2 TCC | 1526 ns | 510 ns |
| Java / C++ / Erlang / v1 | 未测 | 无标准 JSON 或未接入 |

- v2 Release parse 约为 Go 的 **0.50×**（快约 2×），stringify 约 **0.41×**（快约 2.4×）；
- v3 parse 与 Go 基本持平，stringify 略快于 Go，但明显慢于 v2 Release；
- v1/Java/C++/Erlang 没有在同一 harness 中测 JSON，不填假数据。

## 8. 总排名（本次负载）

| 维度 | 排序（快 → 慢） |
|---|---|
| CPU 数组访问 | C ≈ Go ≈ C++ > Java ≈ v2 Release ≫ Erlang > v2 TCC > v3 LTO ≫ v1 |
| 纯求和循环 | C/C++/v2 Release/v3 LTO（被优化掉）> Go ≈ Java > v2 TCC > Erlang ≫ v1 |
| 短生命周期分配 | Go ≈ C ≈ C++ ≈ Java > v2 Release > v3 LTO > v2 TCC > v1 ≫ Erlang |
| 小容器创建 | Erlang > Go > Java > v2 Release > C++ > v2 TCC > v3 LTO > v1 |
| HTTP keep-alive | C ≈ C++ > v2 Release > v2 TCC > v3 LTO ≈ Go > v1 > Java > Erlang |
| WebSocket | Java ≈ v2 Release ≈ v2 TCC > Go > v3 LTO ≫ Erlang（v1 未测） |
| JSON | v2 Release > v3 LTO ≈ Go > v2 TCC（其他语言未测） |

## 9. 结论

1. **v2 相对 v1 是量级提升**：数组约 28×（TCC）/188×（Release）、求和约 83×、对象分配约 5.7×、容器约 9.5×、HTTP 约 1.58×。
2. **v2 Release 已进入 C++/Java 档位**：数组/对象/容器/JSON 接近或超过 Java；HTTP keep-alive 约为 C/C++ 的 92%，WS 吞吐接近 Java 但 p50/RSS 更好。
3. **v3 是 v1 之上的性能工作区**：相对 v1 明显提升（数组约 13.4×、对象约 3.7×、容器约 2.4×），但本次 LTO 构建仍全面慢于 v2；v3 的优化尚未达到 v2 Release 水平。
4. **v2 TCC 仍明显慢于 Release**，适合开发/快速编译；性能场景应使用 `--release`。
5. **v3 列使用其推荐 LTO 配置**；v3 不带 TCC，因此没有 v3 TCC 数据。
6. 跨语言数字受语言语义（RC vs GC、栈/区域、容器实现、HTTP server 实现）影响，表中已标注不等价项。

## 10. 复现

```powershell
# Well v1 / v2 CPU
python E:\Work\WellLang\project\compiler\wellc.py build <bench>.we -o v1.exe
python E:\Work\WellLangV2\project\compiler\wellc.py build <bench>.we --release -o v2.exe

# Well v3：使用 v3 推荐 LTO 配置
$env:WELL_CC = 'E:\Work\WellLangV3\project\tool_ai\clang.cmd'
$env:WELL_CFLAGS = '-O2 -fwrapv -flto'
python E:\Work\WellLangV3\project\compiler\wellc.py build <bench>.we -o v3.exe

# 本次新增的 v1/v2/v3 分配/容器源
project\test\lang_bench\alloc_object.we
project\test\lang_bench\alloc_container_object.we

# HTTP / WS 同轮对比
python project\test\lang_bench\http_compare.py
python project\test\lang_bench\ws_compare.py

# v1 / v3 单独探针
python project\test\lang_bench\v1_http_probe.py
python project\test\lang_bench\v3_http_probe.py
python project\test\lang_bench\v3_ws_probe.py
```
