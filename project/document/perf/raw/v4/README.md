# V4 交替 A/B 原始结果（2026-10-24）

方法：`compiler/tools/perf/ab.py`，每个变体 7 个独立进程，前 2 轮预热，逐轮轮换启动顺序；
表中为保留轮次的 min / median / max。基线二进制由 `git archive 40b5ca9` 导出到
`project/tool_ai/baseline/` 后与本仓库同机编译（同一 TCC / 同一 GCC）。

## 微基准 / 调度 / 数据库

| 文件 | 场景 | 变体 | 指标 |
|---|---|---|---|
| `ab_alloc_tcc.json` | `tools/perf/bench_alloc.we`，2,000,000 次 | V2 基线 vs V4 | `ns_per_iter` |
| `ab_alloc_gcc.json` | 同上，GCC -O2 -fwrapv | V2 基线 vs V4 | `ns_per_iter` |
| `ab_alloc_lto.json` | 同上，V4 `--release` vs `--release-lto` | V4 both | `ns_per_iter` |
| `ab_sched.json` | `tools/perf/bench_sched.c`，300k tasks / 64 oids / 4 workers | V2 基线 vs V4 | `tasks_per_sec` |
| `ab_mysql.json` | `tools/perf/bench_mysql.we`，20,000 次单连接标量查询 | V2 基线 vs V4 | `us_per_query` |
| `ab_literals_tcc.json` / `ab_literals_gcc.json` | `tools/perf/bench_literals.we` | V2 基线 vs V4 | `ns_per_lookup` |
| `ab_event_dispatch.json` | `tools/perf/bench_event.c`，300k events × 2 subscribers | V4 旧 heap snapshot vs inline snapshot | `events_per_sec` |
| `ab_event_pin.json` | 同上 | touch+pin 合并 + 0 订阅者快路径 前后 | `events_per_sec` |
| `ab_loop_tcc.json` / `ab_loop_gcc.json` | `tools/perf/bench_loop.we`（`Host.monoUs`，抗闭式优化） | V2 基线 + clock 补丁 vs V4 | `ns_per_iter` |
| `ab_scalar_tcc.json` / `ab_scalar_gcc.json` | `tools/perf/bench_scalar.we` | 同上 | `ns_per_op` |

## HTTP/WS 正式 5×7

`formal_ab.py`：每个变体 5 个独立进程 × 7 轮，前 2 轮预热，进程间轮换 A/B；
每个进程用 `bench_http.py` / `bench_ws.py`（TCC `-run`，4 workers，8 客户端，3 秒/轮）。

| 文件 | 场景 | 变体 | 指标 |
|---|---|---|---|
| `formal_http.json` | HTTP keep-alive `/bench`（TCC） | V2 基线 vs V4 | `rps`、p50/p99 |
| `formal_ws.json` | WebSocket echo（TCC） | V2 基线 vs V4 | `rps`、p50/p99 |
| `formal_http_release.json` | HTTP keep-alive（GCC -O2 `--release`） | V2 基线 vs V4 | `rps`、p50/p99 |
| `formal_ws_release.json` | WebSocket echo（GCC -O2 `--release`） | V2 基线 vs V4 | `rps`、p50/p99 |

环境：Windows x64、Python 3.12.10、TinyCC 0.9.27、WinLibs GCC 16.2.0（UCRT）、MySQL 5.7.40。

说明：`bench_alloc`/`bench_mysql` 早期版本用 `Host.nowMs()`（Windows 10–15 ms tick）；
后续新增的 `bench_loop`/`bench_scalar` 使用 `Host.monoUs()`（QPC），并把累加器写成数据依赖以
避免 GCC 闭式消除；`bench_literals`、调度与事件基准自带高分辨率计时。
