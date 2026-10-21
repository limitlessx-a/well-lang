# HTTP 解析器性能基线（C vs Well）

> 日期：2026-10-22
> 目的：判断"用 Well 实现 HTTP 协议层"的性能可行性，给出量化基线。

## 1. 方法

- 同一台机器、同一编译器：TinyCC（仓库内 `project/tool_ai/tools/tcc`），与 `wellc` 默认工具链一致。
- 同一个请求样本（约 100 字节，4 个 header + 11 字节 body）：

```text
GET /index.html?a=1&b=2 HTTP/1.1
Host: example.com
User-Agent: well
Accept: */*
Content-Length: 11

hello world
```

- 每组 500 次预热 + 20000 次计时循环，`Host.nowMs()` / `clock()` 取毫秒，除以次数得到 µs/次。
- 三组被测实现：
  1. **C 基线**：手写 C，与 Well 解析器等价的逻辑（request line、target/query 拆分、header 名小写、值 trim、Content-Length body），每次迭代 malloc/free。
  2. **Well 解析器**：`examples/lib/http_parser.we`，经 `wellc` 编译成 C 再交给 tcc。
  3. **Well 分段变体**：只做 request line / 做 header 但不建 Map / 完整 header+Map，用于归因；另有纯字节扫描（无分配）作为下界参考。

复现：

```powershell
cd project/compiler
python wellc.py build examples/http_parser_bench/main.we -o build/bench_well.exe
build/bench_well.exe
```

## 2. 结果

| 实现 | µs / 次解析 | 解析/秒 | 相对 C |
|---|---|---|---|
| C 基线（手写，tcc） | 0.75 | ≈1,250,000 | 1× |
| Well 完整解析器（优化前） | 13.3 | ≈75,000 | ≈18× 慢 |
| **Well 完整解析器（优化后）** | **9.0** | **≈105,000** | **≈12× 慢** |
| Well：仅 request line | 1 | — | — |
| Well：header 解析（不建 Map） | 6 | — | — |
| Well：header + Map | 7 | — | — |
| Well：纯字节扫描（无分配） | 1 | — | — |

说明：C 版本用 `clock()`，20000 次约 15 ms；整数除法下单项 µs 会显示 0，表中数值由总时长换算。

### 2.1 优化后（2026-10-22 第二轮）

- header 解析不再先 `substring` 出整行：用绝对偏移直接在 raw 上切 name/value（每 header 少 2 次 malloc）；
- 原生 `String.toLower()/toUpper()`（单次分配 + 刷新 hash），替代原先 `Bytes` 逐字节拼装（每 header 少约 5 次分配）；
- 结果：13.3 → 9.0 µs/次（约 -32%）。

## 3. 归因

优化后完整解析器 ≈ 9 µs，主要成本仍是**每字段分配**：

- 每个 header 名：`substring` + `toLower`（各 2 次 malloc）+ `Map` 插入；
- 每个 header 值：`substring` + `trim`（各 2 次 malloc）；
- request line / target / path / query / body 各 1–2 次分配；
- 4 个 header ≈ 24 次 malloc/free/次解析，而 C 基线约 8 次。

纯字节扫描 100 字节只花 ≈1 µs（≈10 ns/字节，tcc 未内联的 `well_str_byte_at` 调用），说明**分配才是瓶颈**。

## 4. 结论

1. 优化后的 Well 解析器（每字段分配字符串）比 C 慢约 **12×**，仍不适合直接替换生产路径的 C parser。
2. 继续优化需要语言层支持 **字符串视图/切片**（不复制就能拿 name/value），否则每字段至少 2 次 malloc；在当前 API 下 9 µs 已接近下限（估算 6–9 µs）。
3. 与连接处理开销相比，这个量级可以接受：keep-alive 下单请求约 67 µs，9 µs 约占 13%；解析器并非端到端瓶颈。
4. 因此路线不变：
   - 短期继续用 C parser（成熟、快）；
   - 中期把"原地扫描的 Well 解析器"做成可选实现，用同一套 conformance 测试对照；
   - 长期（等 TLS/keep-alive 等大头落地）再决定是否切换默认实现。

## 4.1 keep-alive 后的端到端吞吐（同机补充测量）

为区分"解析成本"和"连接成本"，对 `GET /ping → 200 ok` 的最小服务器用 Python 客户端做两组测量（各 2000 次请求，统计含 Python 客户端开销）：

| 场景 | req/s | ms/req |
|---|---|---|
| 每次新建连接（Connection: close，旧行为） | ≈175 | 5.72 |
| 单连接 keep-alive（新行为） | **≈15,000** | 0.067 |

- 本机 loopback 的 connect 延迟本身就有 1–15 ms 抖动，旧行为的测量被连接建立淹没；
- keep-alive 后每请求 ~67 µs，其中 C 解析器 ≈0.75 µs；换成优化后的 Well 解析器（9 µs）也只占约 13%，仍在可接受范围；
- 结论：**连接处理（keep-alive / 单连接多请求）对吞吐的影响远大于解析器实现语言**。

## 4.2 C 客户端测量与对象池优化（2026-10-22 第三轮）

Python 客户端自身开销会掩盖优化，故补一个 C 压测客户端（单连接顺序 keep-alive 请求，`QueryPerformanceCounter` 计时，5000 次），并写一个等价的 **raw C echo server**（每连接一线程、阻塞 recv/send）作为机器基线。

| 实现 | req/s | µs/req |
|---|---|---|
| raw C echo server（机器/线程切换基线） | 29k–40k | 25–34 |
| Well HTTP server（对象池优化前） | ≈15.3k | 65.4 |
| **Well HTTP server（对象池优化后）** | **≈23k–28k** | **35–43** |

优化内容：
- 调度器：`WellEventNode` / `WellOidQueue` 空闲链表池（上限 256），消除每次 enqueue/dispatch 的一次 malloc/free；
- HTTP：请求结构体对象池（上限 64）。

结果：Well 服务器每请求 65.4 → ~38 µs（约 **1.8×**），已接近 raw C 基线（差距约 10 µs/请求，主要是 4 个请求字段字符串分配 + 任务派发/线程切换）。

## 5. 附带改动

- `WellString` 增加 `len` 字段缓存字节长度：`s.length`、`s[i]`、`trim/startsWith/endsWith/split` 从 O(n) 降到 O(1)，避免解析循环里的 O(n²)。
- 新增 `String.fromInt(n)`、`String.toLower()/toUpper()`（ASCII，单次分配并刷新缓存 hash）。
