# 调度器下一步：per-worker 队列/锁设计

日期：2026-10-24
状态：设计稿（尚未实现）

## 1. 现状与瓶颈证据

当前 `well_event.c` 的调度器只有一把全局 `g_sched_mutex`，保护：

- `node_pool_take/push` 与 `queue_pool_take/push`
- oid → `WellOidQueue` 的 `qmap`（开放寻址）
- 每个 worker 的 ready 队列/`head/tail`
- `g_pending`、`g_idle_workers`、`g_service_count`
- `sched_wake_one` 的 condvar signal

一次 `well_schedule_task` 在锁内完成节点分配、qmap 探测/创建、入队和唤醒。

本机测量（TinyCC，i5-12400F，2026-10-23/24）：

| 场景 | 结果 |
|---|---|
| 单 oid task 吞吐 W=1 | 1.19M task/s |
| 64 oid task 吞吐 W=4 | 650k task/s（低于 W=1） |
| 2000 次循环/task，W=1 | 493k task/s |
| 2000 次循环/task，W=4 | 820k task/s（1.66×，生产者受限） |

结论：单生产者基准里 **生产者线程自己先饱和**，不能据此判断全局锁；4 producer 基准（`bench_sched_multi.c`）W=1/2/4 为 767k/1.38M/2.08M task/s（W4=2.72× W1）。因此全局锁仍有优化空间，但优先级低于原先判断；per-worker 分片锁可作为后续增量，而不是阻塞项。

补充：8 producer 时 W1/W4/W8 = 716k/1.48M/2.19M；W4 低于 4 producer 的 2.26M，说明 producer 数量 >4 后全局 qmap 锁成为瓶颈，分片锁的收益主要出现在高并发 producer 场景。

HTTP inline 模式绕过了调度器，所以该瓶颈主要影响事件/定时器/并发业务 handler。

## 2. 目标

- W=4 的 CPU 型 task 吞吐 ≥ 3× W=1（当前 1.66×）。
- 单 oid 顺序语义不变：同一 oid 的 task FIFO，且同一时刻最多一个 worker 执行该 oid。
- 空闲时无忙等；退出/清理路径不丢 task。
- 不改变语言层 API 与现有测试语义。

## 3. 方案

### 3.1 分片 qmap + 每 worker ready deque

- 固定 `SCHED_SHARDS = 64`（2 的幂）。
- `shard = hash(oid) & (SCHED_SHARDS-1)`。
- 每个 shard 拥有：
  - `WellLock lock`（短临界区）
  - `qmap`（oid → queue）
  - `ready` deque（该 shard 内可运行的 oid queue）
- 每个 worker 有本地 `injector`（MPSC 或小锁）用于跨 worker 投递。
- 入队：
  1. 计算 shard，锁 shard；
  2. 找到/创建 queue，追加 node；
  3. 若 queue 从 idle 变 runnable，push 到 **当前 worker 的 ready deque**（或 shard ready）；
  4. 解锁，按需唤醒一个 worker。
- worker 循环：
  1. 从本地 ready deque 取 queue；
  2. 若空，扫描其他 shard 的 ready 或从 shard ready 偷一个；
  3. 锁 queue 所属 shard，批量取最多 32 个 node，标记 `running`；
  4. 解锁后执行 task；
  5. 完成后锁 shard，若还有 node 重新 push ready，否则 `running=0`、清 pending；
  6. 所有 shard 空且 `pending==0` 时进入等待。

### 3.2 锁顺序与生命周期

- 固定顺序：**queue 的 shard lock → 本地 ready/injector lock**，禁止反向。
- queue 的 `scheduled/running/cleaning` 标志只在所属 shard 锁内修改；worker 执行 task 时不持锁。
- queue 只有在 `!scheduled && !running && head==NULL` 且 shard 锁内才能回收/池化。
- `g_pending` 改为 per-shard 计数之和，或在全局原子计数（只增删一次/oid 批次）。
- 唤醒：入队后仅在 shard 从空变非空时 signal 一个 worker；避免每 task signal。

### 3.3 工作窃取

- 优先本地 ready；本地空时按 `(worker_id + k) % SCHED_SHARDS` 扫描 shard ready，取一个 queue。
- 窃取只移动 **queue 的调度权**，不移动 node；同一个 oid 仍然串行。
- 负载不均时最多扫描 64 个 shard，成本可接受；可加 `steal_cursor` 避免总从 0 开始。

### 3.4 兼容现有语义

- `well_schedule_task(oid, fn, arg, done)` 签名不变；内部走新入队。
- `done` 回调在 task 执行后、queue 批次结束后调用一次。
- 事件 emit/订阅/TTL 清理仍通过原有 `well_emit`/`qmap` 语义；TTL 清理只操作 oid 组，不直接改 ready 队列。
- 服务持有（`service_hold/release`）与退出条件改为读 per-shard pending + 全局 service 计数。

## 4. 实施阶段

1. **影子实现**：新增 `well_sched2_*`，由 `WELL_SCHED2=1` 环境变量启用；旧路径保留，便于 A/B 回退。
2. **单 worker 正确性**：所有现有调度/事件/TTL/HTTP/WS 测试在 `WELL_SCHED2=1` 下通过。
3. **多 worker 正确性**：`37_workers_fifo`、work stealing、panic 隔离、force cleanup 用例。
4. **性能门槛**：W=1 不退化 >5%；W=4 CPU task ≥3× W=1；64 oid ≥2.5×。
5. **默认切换**：连续 30 分钟 HTTP/WS soak 无错误、RSS 稳定后，删除旧路径。

## 5. 风险

- 丢唤醒：所有“入队→signal”必须发生在 shard 状态变更后；等待循环用谓词重查。
- queue 复用竞态：`cleaning` 期间拒绝新 node，或把 node 转移到新 queue。
- 退出死锁：worker 退出前必须处理完所有 shard pending。
- 内存：每 shard qmap/ready 固定开销约几十 KB，可接受。

## 6. 验收基准

- `bench_sched.c`（trivial task / `-DTASK_WORK=2000`）W=1/4/8。
- `bench_http.py` 8/12 客户端（事件路径改为非 inline 时）。
- 30 分钟 `bench_soak.py`：0 错误，RSS 斜率 ≤1MB/h。
