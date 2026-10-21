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
| Well 完整解析器 | 13.3 | ≈75,000 | ≈18× 慢 |
| Well：仅 request line | 1 | — | — |
| Well：header 解析（不建 Map） | 6 | — | — |
| Well：header + Map | 7 | — | — |
| Well：完整（含 body/结构体） | 13 | — | — |
| Well：纯字节扫描（无分配） | 1 | — | — |

说明：C 版本用 `clock()`，20000 次约 15 ms；整数除法下单项 µs 会显示 0，表中数值由总时长换算。

## 3. 归因

完整解析器 ≈ 13 µs，主要成本不在"扫描字节"，而在**每字段多次分配**：

- 每个 header 名：`substring`（2 次 malloc）→ `lowerAscii`（`Bytes` 新建 + 增长 + `toString` 2 次 malloc）→ 存入 `Map`（hash + 数组扩容）；
- 每个 header 值：`substring` + `trim`（各 2 次 malloc）；
- 4 个 header ≈ 30+ 次 malloc/free/次解析，而 C 基线约 8 次。

纯字节扫描 100 字节只花 ≈1 µs（≈10 ns/字节，tcc 未内联的 `well_str_byte_at` 调用），说明**分配才是瓶颈**。

## 4. 结论

1. 当前 Well 解析器（每字段分配字符串）比 C 慢约 **18×**，不适合直接替换生产路径的 C parser。
2. 但把实现改为**按字节原地扫描 + 只物化最终需要的字段**后，预期可到 **3–5 µs/次**（≈4–6× C）：扫描 1 µs + 约 10 次结果字符串分配（~2–3 µs）。
3. 与连接处理开销相比，这个量级可以接受：一次 HTTP 请求的 accept/读/任务派发/写/关闭 在毫秒级，13 µs 的解析占比 <2%；即使在优化后仍如此，解析器并非端到端瓶颈。
4. 因此路线不变：
   - 短期继续用 C parser（成熟、快）；
   - 中期把"原地扫描的 Well 解析器"做成可选实现，用同一套 conformance 测试对照；
   - 长期（等 TLS/keep-alive 等大头落地）再决定是否切换默认实现。

## 5. 附带改动

- `WellString` 增加 `len` 字段缓存字节长度：`s.length`、`s[i]`、`trim/startsWith/endsWith/split` 从 O(n) 降到 O(1)，避免解析循环里的 O(n²)。
- 新增 `String.fromInt(n)`，便于拼装响应/日志。
