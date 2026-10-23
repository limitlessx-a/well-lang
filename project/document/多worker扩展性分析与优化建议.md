# 多 worker 扩展性问题分析与优化建议

> 状态：分析/建议（待用户确认场景后细化实施）。日期：2026-10-23。触发：用户实测「4 worker 反而更差」。
> 关联：`core/well_event.c` 调度器、`core/wellrt.c` 分配/registry、`core/well_model.c` TTL、`runtime/modules/http/well_http.c`、`document/per-oid-arena-实施方案.md`、`document/待开发功能-优先级.md`（§0.2 第 3 项）。
> 本文先给出**可直接执行的定位方法**与**按性价比排序的修改建议**；所有代码位置为当前 HEAD。

## 0. 先分类：你说的「更差」是哪个指标？

「4 worker 更差」可能是四种完全不同的现象，修法不同：

| 现象 | 典型原因 | 先做的检查 |
|---|---|---|
| **吞吐（req/s）变低** | 客户端瓶颈/机器噪声；调度锁竞争；分配器锁竞争；单热点串行 | 客户端 CPU 是否打满；server CPU 利用率；p50 是否变差；换 C 客户端/多进程 |
| **p50/p99 延迟变高** | 每请求跨线程 handoff（reader→worker）与唤醒延迟；worker 落点随机/冷缓存 | 单连接串行延迟；worker=1 vs 4 的 p50；是否每请求被调度到不同 worker |
| **CPU 占用异常** | 广播唤醒（thundering herd）、忙等/自旋、后台 TTL 扫描 × worker 数 | 空载 CPU；每请求唤醒次数；worker 线程 CPU 分布 |
| **业务侧更慢（Model/MySQL/文件）** | 单热点串行：MySQL 默认连接锁、同一 oid FIFO、同一 Cache 行/分片锁、同一 static 写 | 是否所有请求操作同一个 oid/同一条连接/同一行数据 |

> 本机初步数据（8 逻辑核 Windows x64，TinyCC，Python 客户端）显示：**纯 CPU 型 handler 的吞吐随 worker 数近似线性提升（W1 38 → W4 112–138 rps）**，说明调度器本身能并行；而轻量 HTTP/分配型 handler 的差异在 ±30–40% 噪声内，不能仅凭一次测量判定「4 worker 更差」。**请对照上表确认你的场景**，再按 §4 选择改法。

## 1. 本机实测（供对照，非绝对结论）

测试脚本：`tool_ai/tmp/worker_scale/bench_worker.py`（keep-alive、固定 CPU/分配负载的 Well HTTP server；`WELL_WORKERS` 由环境变量控制）。条件：8 逻辑核，Windows，TinyCC，Python 客户端线程。

| 场景 | W=1 | W=2 | W=4 | 结论 |
|---|---|---|---|---|
| CPU 型（handler 内 200k 次循环） | 38 rps | 67–73 | 112–120（单次 137） | **随 worker 扩展**（≈3×） |
| 轻 HTTP（`bench.py` 模板，仅 respond） | 18–33k rps | 21–26k | 18–26k | 噪声主导；p50 恒 50µs；客户端≈服务端同规模 |
| String 拼接型（100 次 `+`） | 12.6k | 20.3k | 23.4k | 随 worker 提升（W1 被单 worker 限制） |
| Bytes/pool 型（`new Bytes()` + 200 push） | 15.8–21.5k | 20.4k | 15.4–22.2k | 重复测量后差异≈噪声，但存在**全局池锁竞争**风险 |
| 单连接串行 + 分配型 | 7.0–9.6k | — | 7.2–8.3k | 无增益；每请求 handoff/唤醒开销可见 |

**结论**：不要仅凭轻负载吞吐判断 worker 数；先按 §2 用更可靠的测量定位。若业务 handler 是 CPU/独立 oid 型，4 worker 应更快。

## 2. 可靠的测量方法（先把「更差」测准）

1. **每配置 ≥5 次**，报告 **中位数 + min/max**，不要单次比较；关闭后台构建/杀毒扫描。
2. **分离客户端与服务端 CPU**：客户端用多进程（避免 Python GIL）或 C 客户端；`Get-Process <server>.exe | Select CPU` 采样；客户端 CPU 打满时结论无效。
3. **分别测三类工作负载**：CPU 型、纯分配型、真实业务（Model/MySQL/文件）；不要用同一个模板下结论。
4. **记录 p50/p99 与吞吐**，不只吞吐。
5. **客户端并发 > worker 数**（否则测的是单连接串行 handoff），并单独测「单连接」延迟。
6. **固定端口/清理残留 server**；端口复用会连到旧进程，结论全错（本轮已遇到）。
7. 每轮重启 server 进程，避免 TTL/对象池状态影响。

## 3. 代码层面的确定性问题（按影响）

### 3.1 每请求广播唤醒（thundering herd，确定存在）

`well_event.c` `sched_notify()`（约 `:775-784`）：当 `g_pending == 0 && g_service_count == 0` 时 **broadcast 唤醒所有 worker**；短任务服务器每完成一个请求 `pending` 就回到 0 → 每个请求唤醒全部 worker。W=1 无此开销，W=4 每请求多 3 次唤醒/上下文切换。**建议**：主 worker 用独立条件变量/`g_main_waiting` 标志，仅在其可能退出时唤醒；普通路径只 `signal` 一个 worker。（Lead 原型验证：改动后单次 A/B 噪声仍大，但代码上可确定减少无效唤醒；建议作为第一批小改动并配合 §2 测量。）

### 3.2 每个请求都在全局调度锁内做多步操作

`well_schedule_task`（`:669-704`）在 `g_sched_mutex` 内做 `node_pool_take`（池锁/分配）、`qmap_get/create`、`ready_push`、`cond_signal`；`worker_take`（`:496-508`）在**同一把锁**内做本地弹出 + **线性扫描所有 worker 偷取**；任务完成后再加锁 `node_pool_put`、`qmap_remove`、`sched_notify`。W 越大，锁竞争与临界区成本越高。**建议**：把 node 分配/回收移出锁（worker 本地 free-list/TLS）；`qmap` 分片；偷取改为随机 victim + 尝试锁；批量取任务。

### 3.3 请求落点按「唯一 oid」哈希，缺少连接亲和

HTTP 每请求用 `req->id` 作调度 oid（`well_http.c:933` 附近），`ready_push` 按 `qhash(oid) % worker_count` 选 worker（`well_event.c:469-482`）。由于 req id 每次都不同，**同一连接的连续请求会落到不同 worker**：冷缓存、跨核迁移、每请求一次唤醒。**建议**：连接亲和 —— accept 时给连接分配 `wid`（如 `conn_id % W`），reader 线程用新 API `well_schedule_task_on(oid, fn, arg, done, wid)` 投递到该 worker 的本地队列；work stealing 兜底。W 增大时收益更明显。

### 3.4 分配器锁竞争（与 per-oid arena 计划直接相关）

- `well_new` 小对象走全局 size-class 池 `pool_take`（`wellrt.c:144-157`，锁 `g_pool_lock:119`），每次 `new Bytes()/struct/event/容器` 都过全局锁；`well_obj_free`（`:159-170`）归还也加锁。
- registry add/take（`wellrt.c:59-90`）按分片锁；每对象一次。
- `String` 走 `malloc/free`（`wellrt.c:885-904`），Windows 进程堆锁在高并发分配下会争用。
- Bytes 扩容是几何的（`bytes_reserve:1066`），本身没问题，但每请求 `new Bytes()` + `toString()` 仍触发 pool/registry/malloc。
  **建议**：先做 **per-worker/线程本地 size-class free-list**（全局池只在批量 refill/溢出时加锁）；再按 `per-oid arena` 方案把 oid 拥有对象改 bump 分配（见该计划 §5）；String 可评估线程本地小对象缓存。预期收益：分配型 handler 的 W=4 吞吐提升、CPU 抖动下降。

### 3.5 默认 worker 数偏大

`WELL_WORKERS` 未设置时默认 `max(1, cores*3/4)`（8 逻辑核 → 6）。对轻量/低并发/延迟敏感服务，**更少 worker（2–4）通常更好**；worker 数应按「并发连接数/业务串行度」调，而不是按核数。**建议**：文档和 bench 指南明确推荐值；考虑默认 `min(4, max(1, cores/2))`，高 CPU 型可显式调大。注意 worker 数在启动后固定（`well_worker_count`），需要重启生效。

### 3.6 单热点串行（业务侧最容易被误判为「worker 越多越慢」）

- **MySQL 默认连接**：修复 M11 后 `g_default` 有全局锁，所有 `Mysql.exec/queryJson` 串行；4 worker 只是让 4 个线程排队抢同一把锁 → 吞吐不升反降。**建议**：业务改用连接池（`Mysql.pool`）或多连接 API，并在文档说明默认连接是单连接串行。
- **同 oid FIFO**：若所有请求最终操作同一个业务 oid（如固定的 `Cache` 行、同一 struct 对象），调度器会严格串行该 oid；worker 再多也没用。**建议**：拆分 oid（每请求/每用户一个 oid）或把热点数据拆成子 oid。
- **同一 Cache 分片 / static 写**：分片 spinlock 命中冲突时同样串行。
  先确认你的 handler 是否操作同一 oid/同一 MySQL 默认连接——这比调度器更常见。

### 3.7 客户端/测量因素

`tools/bench.py` 的 HTTP 压测客户端是 Python 线程，8 核机器上 4–16 客户端本身就可能占满 CPU；此时 worker 数变化不可见或变噪声。用 C 客户端/多进程才能测服务端上限。

## 4. 推荐修改顺序（按性价比）

### 第一批（1–3 天，低风险，先做）
1. **`sched_notify` 去广播**：主 worker 独立条件/标志，普通路径只用 `signal`；消除每请求全 worker 唤醒。
2. **连接亲和**：HTTP/WS accept 时分配 worker，新增 `well_schedule_task_on(..., wid)`，reader 固定投递；steal 兜底。
3. **减少临界区**：node 分配移出 `g_sched_mutex`（worker 本地 free-list）；`qmap` 分片；任务完成后的 `qmap_remove` 延迟/批量。
4. **worker 数策略+文档**：默认与推荐值写清楚；bench 指南要求「客户端并发 > worker 数、多种负载、5 次取中位」。
5. **先修测量**：新增 `WELL_STATS` 输出（任务排队/等待时间、每 worker 忙碌度、队列长度、锁等待）——没有数据不继续改。

### 第二批（1–2 周，中风险）
6. **per-worker 池/本地 free-list**（well_new/well_obj_free 热路径去全局锁）。
7. **per-oid arena**（独立计划），优先覆盖 struct/event/容器/Bytes；降低 registry/pool 压力。
8. **String 小对象缓存**（线程本地）或字符串尺寸分级池，降低 malloc/free 竞争。
9. **HTTP 解析移入 worker**：reader 线程只读原始字节，任务内完成解析+handler+respond，减少一次跨线程 handoff（或提供「短 handler 在 reader 线程内联执行」的可选模式，需语义评审）。

### 第三批（结构性，2–4 周）
10. **分片调度器**：每 worker 独立运行队列锁 + 原子 idle/steal 标志 + 分片 qmap + trylock/退避偷取 + 批量 dequeue。
11. **锁-free/MPSC 投递**（reader→worker 单生产者单消费者队列），彻底去掉调度锁热路径。
12. **MySQL 默认连接改造**（每 worker 一个默认连接或强制池化），避免单锁串行。

## 5. 验收（建议）

| 指标 | 目标 |
|---|---|
| 轻 HTTP 吞吐（C 客户端，W=1/2/4/8） | W=4 相对 W=1 提升 ≥20%（当前噪声内无提升） |
| CPU 型吞吐 | 保持现有 ≈3× 扩展，不回归 |
| 分配型（Bytes/String）W=4 | 相对 W=1 提升 ≥30%，无明显锁争用抖动 |
| p50/p99（单连接、多连接） | p50 不劣化 >10%，p99 不劣化 >20% |
| 空载 CPU | worker 空闲时接近 0（无自旋/广播） |
| 回归 | `tools/verify.py` 188/188 + 18 C 通过；TTL/pin 压力通过 |

## 6. 与 per-oid arena 计划的关系

两者解决不同瓶颈，**互补**：

- **调度/handoff**（本文件 §3.1–3.3）：影响轻量、低延迟、连接型负载 → 先做连接亲和 + 去广播 + 缩临界区。
- **分配器**（§3.4）：影响分配型/对象型负载 → per-worker pool cache + per-oid arena。
- **单热点**（§3.6）：不是分配/调度问题，需要业务拆 oid/池化连接，语言层无法自动解决。

建议顺序：**先修测量与第一批调度改动** → 用真实负载确认瓶颈 → 再决定 arena 与分片调度器的投入；不要为了「4 worker」盲目改默认 worker 数。

## 7. 结论（直接回答）

1. **先别把结论定在「4 worker 有 bug」**：本机数据里 CPU 型负载 4 worker 是 1 worker 的 ~3×，轻量 HTTP 的「更差」在噪声区间内。请给出你的测试命令/场景（handler 做什么、clients/seconds、是否用 MySQL/同一 oid、客户端语言），我可以按 §2 复测定位。
2. 若确认是**轻量/短任务 + 高并发**：优先做 **连接亲和 + 去广播 + 减少全局锁临界区 + worker 数默认下调**（第一批）。
3. 若确认是**分配/对象密集**：优先做 **per-worker 池 + per-oid arena**（第二批）。
4. 若确认是**业务侧同一 oid/同一 MySQL 默认连接**：那是串行点，改业务数据分片或连接池，加 worker 只会更糟。
5. 无论哪种，先加指标（任务等待/worker 忙碌/锁等待），否则无法验证改动。

## 附录：本次复现资产

- 脚本/服务：`tool_ai/tmp/worker_scale/`（`bench_worker.py`、`cpu_server.we`、`alloc_server.we`、`str_server.we`、`bytes_server.we`）
- 复现：`python tool_ai\tmp\worker_scale\bench_worker.py <exe> <workers> <clients> <seconds> <port> <path>`
- 关键代码位置：`well_event.c:469-508,669-704,775-784,786-839`；`wellrt.c:59-90,119,144-170,885-904,1066`；`well_http.c:933`（HTTP 任务投递）
