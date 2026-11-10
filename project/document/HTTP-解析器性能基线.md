# HTTP 解析器性能基线（C vs Well）

> 日期：2026-10-22（数据采集日）
> 目的：判断"用 Well 实现 HTTP 协议层"的性能可行性，给出量化基线。
>
> **复核说明（2026-10-23 补记，Lead 委托）**
>
> - 本文所有数值均为 **2026-10-22 的历史基线**：Windows x64 + 仓库内 TinyCC 0.9.27（driver 默认，不加 `-O2`）、本机 loopback、多轮交替取样；文中已注明单次测量波动 20–30%，读数只用于**量级与相对关系**判断。
> - **当前实现已变化**（2026-10-23 核心代码审计与修复后为 188 `.we` + 18 C；2026-11-10 起主线 V4 为 190 `.we` + 19 C）。下列结论的**定性判断仍然成立**（Well 解析器比等价的 C 实现慢一个数量级、分配是主要成本、端到端瓶颈在连接/线程而非解析），但**不应把文中的绝对 µs/req 当成当前值**。本轮修复对性能相关行为的影响见 §6。
> - 相关资产仍存在且可用：`compiler/examples/http_parser_bench/main.we`、`compiler/examples/lib/http_parser.we`、用例 `65_well_http_parser` / `66_http_parser_route`、`compiler/tools/bench.py` 的 `parser` 场景（已核对文件存在与用例名）。
> - 复跑方法见 §1「复现」与 `compiler/tools/README.md`；若要作为当前基线，请另存新数据并注明机器/编译器/负载。

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

> 复现说明（2026-10-23 补记）：
> - 程序输出 `iters / total_ms / us_per_parse / parses_per_sec / checksum`；默认 20000 次计时 + 500 次预热，属短跑（秒级以内），不是长压测。产物写在 `compiler/build/`（gitignored）。
> - `tools/bench.py --scenario parser` 测的是**编译器前端**（`wellc` 的 `lex` + `parse_program`，对同一份 `bench_http_server.we`，单位 µs/parse），与本节的 **Well 运行时 HTTP 解析器**微基准**不是同一口径**，不能直接比较；`compiler/tools/README.md` 有 bench 场景、基线保存与 `--compare` 阈值说明。
> - 在当前代码上复跑时，请记录 `WELL_CC`、`WELL_CFLAGS`/优化级别、CPU 型号与机器负载，同一配置交替跑 ≥3 轮取最小值；单次读数受本机抖动影响可达 20–30%。

## 2. 结果

> 以下表格与 §4.x 的吞吐/延迟数字均为 **2026-10-22 历史测量**（条件见文首复核说明），仅用于相对比较。

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

> 注：§4.1–§4.5 的 req/s、µs/req 均为 2026-10-22 的历史测量（同机同工具链），只用于相对比较；当前实现见 §6。

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
| **Well HTTP server（对象池优化后，初测）** | **≈23k–28k** | **35–43** |

优化内容：
- 调度器：`WellEventNode` / `WellOidQueue` 空闲链表池（上限 256），消除每次 enqueue/dispatch 的一次 malloc/free；
- HTTP：请求结构体对象池（上限 64）。

结果：对象池把每请求 65.4 µs 降到约 38–43 µs（约 **1.7×**）。

## 4.3 更严格的取样与请求行 token 复用（2026-10-22 第四轮）

本机负载抖动较大（同一实现单次测量可差 20–30%），因此改为：**5000→20000 请求、raw/Well 交替测多轮，同时报告最小值与平均值**。结论以"最小 µs/req"（受干扰最小）为准。

| 实现（20000 请求，4 轮交替） | 最小 µs/req | 平均 µs/req |
|---|---|---|
| raw C echo server | **25.1** | 29.5 |
| Well HTTP server | **40.7** | 43.0 |

- Well / raw C 比值 ≈ **1.45–1.6×**；差距约 15.6 µs/请求，主要来自：每请求 4 个字段 String 的分配（约 8 次 malloc/free）、事件对象创建/注册、以及 reader→worker→reader 的两次线程切换；
- 本轮顺带优化：请求行 token（GET/POST/…）与空 query/body 复用内建字符串，非空字段才分配；收益在噪声范围内（约 1–2 µs），保留。

进一步降低需要结构性改动（把 handler 内联到 reader 线程的快速路径，或语言层字符串视图），已列入待确认项。

## 4.4 多连接并发扩展性（2026-10-22 第五轮）

方法：N 个 C 客户端进程（各自单连接 keep-alive）同时压同一服务器，聚合 req/s；每配置取 3 次。

| 实现 | 1 客户端 | 4 客户端 |
|---|---|---|
| raw C echo server（参考） | 35.8k | 86.6k |
| Well HTTP，workers=1（默认） | 17.8k–25.2k | **42k–58k** |
| Well HTTP，workers=4 | 21k–27k | 19k–35k（噪声大） |

结论与优化：
- 短 handler 场景下默认 `workers=1` 反而最好；多 worker 在 4 连接下线程超订（4 conn + 4 worker + 4 客户端进程）且调度器是单锁，收益为负；
- 本轮优化"同步响应快速路径"：http task 用 TLS 记录当前请求，`Http.respond` 直接使用该指针（跳过全局请求表锁），并把请求引用统一交给连接线程释放；4 客户端聚合从约 37k 提升到 **42–58k**；
- 调度器 per-worker 锁 / 更精细的唤醒属于后续结构优化（待确认）。

## 4.5 连接级路由缓存（2026-10-22 第六轮）

每次请求原本都要拿模块全局锁做 `method+path -> route` 扫描。keep-alive 连接上通常反复访问同一路径，因此**每条连接缓存上一次的 method/path/route**，命中就完全跳过全局锁。

- 实现：`WellHttpConn` 增加 `cache_method[16] / cache_path[256] / cache_route / cache_valid`；连接线程先查缓存，未命中再走 `http_find_route` 并回填；
- 4 客户端聚合（workers=1）：**58.3k / 60.1k / 59.1k req/s**（上一轮 42–58k，约 +15%）；单客户端不变（21–25k，噪声内）；
- 单客户端延迟仍受字段分配与线程切换限制（§4.3），路由锁不是主要成本，收益集中体现在并发连接场景。

## 5. 附带改动

- `WellString` 增加 `len` 字段缓存字节长度：`s.length`、`s[i]`、`trim/startsWith/endsWith/split` 从 O(n) 降到 O(1)，避免解析循环里的 O(n²)。
- 新增 `String.fromInt(n)`、`String.toLower()/toUpper()`（ASCII，单次分配并刷新缓存 hash）。

## 6. 2026-10-23 审计修复对性能相关行为的影响（补记）

本轮（核心代码审计 P0/P1/P2/P3 修复）**没有重新跑长压测**；以下为代码级影响分析，用于说明本文历史结论与当前实现的差异来源。

### 6.1 HTTP 模块（运行期，端到端）

- 头部总期限 `WELL_HTTP_HEADER_TIMEOUT_MS = 5000`：每请求多一次时钟比较，代价可忽略；作用是拒绝慢速头（slowloris）。
- 连接上限 `WELL_HTTP_MAX_CONNECTIONS = 1024`：超限返回 503 并关闭，避免 1 连接 1 线程 + 64KB scratch 被耗尽；压测并发在数十连接以内无影响。
- 严格 `Content-Length` 解析（拒绝负数/溢出/重复/尾随垃圾）：解析分支略增，安全收益远大于性能成本。
- keep-alive 残余字节保留（滚动缓冲）：同一 TCP 段内到达的后续请求不再被丢弃，pipelining 由“静默挂起 15 s”变为正确响应；本文压测是顺序请求，结论不受影响。
- 504 改为 `Connection: close`；新增发送超时 `SO_SNDTIMEO = 15 s`，慢读客户端不再永久阻塞 responder。
- `examples/lib/http_parser.we`（Well 解析器）**本轮未改动**（文件最后修改时间 2026-10-22）；服务端默认仍是 C 解析器，§4 的路线结论不变。

### 6.2 生成代码（影响 Well 解析器的微基准）

- **实参左到右求值**（`wellc/codegen.py: gen_call_ordered`，`:789`）：跨函数/内建调用先把每个实参提为临时变量（C 语句表达式 `({ cty _agN = ...; ...call...; })`），保证 TCC 与 GCC 语义一致。对调用密集的解析器代码，每调用多若干条临时赋值，单元微基准可能比本文历史值略有上升（未实测）；这是为“同一程序跨编译器结果一致”付出的可解释成本。
- **循环/块级 temp journal 释放**（`gen_scoped` / `push_scope` / `pop_scope`，`:843`；循环/`for-of` 体在 `:1005`、`:1046` 各建 mark 并 `well_temp_release_to`）：临时对象在作用域/迭代结束时释放，长时间解析循环的内存峰值显著下降（旧实现会累积到函数末尾，journal 满 1024 后还会静默漏释放），短循环开销仅每作用域 1–2 次整型操作。
- **C 标识符修饰**（`c_var/c_field/c_func`，`:32-40`；`v_/f_/wf_` 等前缀）：只改变生成符号名，消除与 C 关键字、libc、运行时符号的冲突，**不影响运行期性能**（可能轻微改变编译产物布局/符号表）。
- **for-of 迭代源只求值一次**（`:1016-1030`）：迭代源表达式在循环前求值一次并按所有权释放（旧实现每轮 length/get 重复求值并泄漏容器）。解析器若用 for-of 遍历数组/Map，行为更正确且更快。
- 其余修复（引用计数泄漏、switch/作用域、panic 定位等）主要修正正确性/内存，不改变本文“分配是解析器主要成本”的判断。

### 6.3 复跑建议

- 微基准：按 §1 命令构建并运行 `examples/http_parser_bench`（默认 20000 次，秒级以内），同配置交替 ≥3 轮取最小值，记录 `WELL_CC`/flags/CPU/负载。
- 端到端：`python tools/bench.py --scenario http --iterations 2000 --clients 1 --seconds 2`（短跑），或 `--save-baseline` 后用 `--compare` 与历史基线做 ±30% 阈值的回归判断（见 `compiler/tools/README.md`）。
- 若要形成**当前**基线，建议新建 JSON 基线文件（`compiler/build/bench/`，gitignored）并在文件名/提交说明中注明采集日期、机器、编译器与负载；本文数值仅作历史参照。
