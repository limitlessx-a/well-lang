# Well 线程模型与 Java 虚拟线程对比

> 本文描述当前 Well 运行时的多线程实现，并与 Java Virtual Threads 做对比。
> 当前基线：89 个 .we 用例 + C 测试通过。

---

## 1. 结论摘要

Well 当前是：

```text
固定数量 OS 线程 worker 池
+ 每 oid 事件队列
+ 同 oid 单 runner
+ per-worker local deque
+ work stealing
+ handler run-to-completion
```

Java Virtual Threads 是：

```text
JVM 调度的用户态虚拟线程
+ continuation
+ 可挂起/恢复
+ 阻塞时 unmount carrier
+ ForkJoin 风格调度
```

核心区别：

> **Well 的“任务”是绑在 OS worker 栈上的 run-to-completion handler；Java 虚拟线程是可挂起/恢复的用户态线程。**

---

## 2. Well 当前线程实现

### 2.1 线程池

入口：

```c
well_run_loop()
```

配置：

```text
WELL_WORKERS = 1..64
默认 1
```

线程组成：

```text
主线程 worker id = 0
worker 线程 id = 1..N-1
```

主线程也参与执行。

平台封装：

```text
Windows: CreateThread
POSIX:   pthread_create
```

### 2.2 调度单元

调度单元不是“线程”，而是 **oid 队列**：

```text
oid -> FIFO event queue
```

规则：

- 同一个 oid 同时只有一个 runner；
- 同一个 oid 的事件严格 FIFO；
- 不同 oid 可以并行；
- oid 是事件的目标对象/执行分组键。

事件模型：

```text
emit(event)
  -> 按 event.oid 入对应队列
  -> 队列 ready 后交给某个 worker
```

### 2.3 Work stealing

当前已实现：

```text
worker 0: local deque
worker 1: local deque
...
global injector
```

分配：

- 新 ready oid 按 `hash(oid) % worker_count` 分配到某个 worker local deque；
- worker 优先取本地 deque 尾部（LIFO）；
- 本地空时，从其他 worker 的 deque 头部偷（FIFO，偷最老）；
- 其他都空时，取 global injector；
- local deque 是双向链表，头尾 O(1)。

限制：

- 当前所有 local deque 操作仍由 **一把 `g_sched_mutex`** 保护；
- work stealing 是 oid 队列级，不是事件级；
- 同一 oid 内的事件不会拆分到多个 worker。

### 2.4 handler 执行

worker 取到 oid 队列后：

```text
1. q->running = 1
2. 取队首 event
3. 调用 dispatch_event
4. 执行 handler
5. panic 隔离
6. 清理 frame
7. event release
8. q->running = 0
9. 队列非空则重新入 ready
```

handler 是 **run-to-completion**：

- 不能中途暂停；
- 不能迁移到另一个 worker；
- 长 handler 会占住一个 worker；
- 当前没有抢占、没有 fiber、没有 safepoint。

### 2.5 并发安全

已实现：

- Cache：64 分片锁；
- oid registry：分片锁；
- 订阅表：锁 + 快照派发；
- print：全局串行化锁；
- RC：原子计数；
- oid exec_ref：执行引用租约；
- handler panic：线程局部 setjmp/longjmp；
- 事件队列：scheduler mutex；
- 对象池：池锁；
- 活动表：`g_act_lock`。

### 2.6 HTTP / WebSocket / Timer 的线程现状

HTTP、WS、Timer 都已接入 oid 调度器，MySQL 尚未接入：

```text
HTTP listener thread
  -> accept，每个连接一个 reader thread

HTTP conn reader thread
  -> 解析请求 + keep-alive 循环（读完一个请求等响应写出，再读下一个）
  -> well_schedule_task(req.oid, route_fn)   // 投递到请求 oid 的队列
  -> 超时/关闭：连接线程自己关闭 socket

WS listener thread
  -> accept，每个连接一个 reader thread（握手 + 收帧）
  -> well_schedule_task(conn.oid, handler_fn) // open / message / close 都是 task

Timer thread（core，懒启动）
  -> 约 5ms 轮询到期 timer
  -> well_schedule_task(timer.id, handler_fn) // 每次到期一个 task

worker（oid runner）
  -> handler（run-to-completion）
  -> 可 emit 业务事件，由另一个 oid 的 handler 调用 Http.respond / Ws.send 完成回写
```

因此：

- HTTP/WS/Timer handler 都跑在 worker 池，遵守同 oid 单 runner；
- listener 只做 accept，连接之间完全并行（`59_http_concurrent` / `61_ws_concurrent` 覆盖）；
- HTTP 连接支持 keep-alive（HTTP/1.1 默认，15s 空闲关闭），同一连接上的请求串行、连接之间并行；
- `Http.respond`、`Ws.send/close` 可从任意 handler/线程调用；HTTP 首个响应生效，WS 按连接表查 clientId；
- Timer 到期以 timer id 为 oid 投递 task；有 timer 时 timer 线程持有 service 计数，进程不会提前退出；
- 5s 内无人响应的 HTTP 请求由清扫线程返回 504 并关闭连接；
- 每连接/每请求都有引用计数与 service 计数，run loop 不会在连接仍存活时退出；
- 每连接一个线程：高并发连接数受线程数限制（后续可换 I/O 多路复用）；
- MySQL 仍是同步调用，是当前多线程模型剩余缺口。

### 2.7 当前没有的能力

- 无虚拟线程；
- 无 fiber / ucontext；
- 无 handler 抢占；
- 无 reduction / safepoint；
- 无阻塞 I/O unmount；
- 无动态 worker 扩缩容；
- 无 work stealing 指标；
- 无长 handler 监控；
- 无 per-worker 独立锁；
- 无线程优先级/公平调度。

---

## 3. Java Virtual Threads 简述

Java 虚拟线程（JEP 444）是 JVM 管理的轻量线程：

- 大量虚拟线程可以复用少量 carrier OS 线程；
- 虚拟线程栈按需增长，存储在堆上；
- 阻塞操作（如 socket read）会 **unmount** 虚拟线程，释放 carrier；
- 恢复时重新 mount 到某个 carrier；
- 调度器通常是 ForkJoinPool；
- 采用 continuation 保存/恢复执行位置；
- 不是“任意指令处抢占”，而是在阻塞点/JVM safepoint 等位置协作切换；
- 使用 `Thread.ofVirtual()` / `Executors.newVirtualThreadPerTaskExecutor()`。

关键点：

> 虚拟线程是“任务”，不是 OS 线程；Well 的 handler 是 C 调用栈上的执行片段。

---

## 4. 对比表

| 维度 | Well 当前 | Java Virtual Threads |
|---|---|---|
| 调度对象 | oid 事件队列 | 虚拟线程 |
| 执行载体 | 固定 OS worker 线程 | carrier OS 线程池 |
| 任务执行方式 | run-to-completion | 可挂起/恢复 |
| 暂停/恢复 | 不支持 | continuation |
| 抢占 | 无 | 阻塞点协作式，不是任意抢占 |
| 阻塞 I/O | 占住 worker | unmount，释放 carrier |
| 任务迁移 | 不迁移 C 栈 | 可迁移到其他 carrier |
| 并发单位 | oid | 虚拟线程 |
| 同 oid 顺序 | 严格 FIFO | 无内建 oid 语义 |
| 异 oid 并行 | 支持 | 由用户创建线程决定 |
| 调度器 | 自实现 worker 池 + work stealing | JVM/ForkJoin |
| 栈 | OS 线程栈 | 堆上虚拟线程栈 |
| 创建成本 | 事件队列/队列节点 | 虚拟线程对象 + continuation |
| 数量级 | worker 1..64 | 可百万级虚拟线程 |
| 阻塞语义 | 阻塞 worker | 阻塞时让出 carrier |
| 长任务公平性 | 无 | 依赖阻塞点/调度 |
| HTTP 集成 | 请求作为 task 进 oid 队列（listener 不阻塞） | 可用虚拟线程每请求 |
| GC/RC 关系 | oid exec_ref + RC | GC 管理堆，线程无 RC |

---

## 5. 关键差异详解

### 5.1 任务模型不同

Well：

```text
event -> oid queue -> worker 执行 handler
```

handler 是普通 C 函数调用。

Java：

```text
virtual thread -> continuation -> carrier
```

虚拟线程可以：

```java
socket.read();   // 阻塞时 unmount
```

恢复后继续执行下一行。

Well 的 handler 如果阻塞：

```c
read(fd, ...);   // 占住 worker
```

worker 不能执行其他 oid 事件。

### 5.2 同 oid 串行是 Well 的核心

Well 用 oid 保证：

```text
同 oid FIFO
同 oid 单 runner
异 oid 并行
```

Java 虚拟线程没有 oid 概念。  
如果要实现同样语义，需要用户自己加锁/队列。

### 5.3 Work stealing 的粒度不同

Well：

```text
偷 oid 队列
```

Java ForkJoin：

```text
偷任务/continuation
```

Well 不会把同一 oid 内的事件拆给多个 worker，因为那会破坏 FIFO/单 runner。

### 5.4 没有 continuation

Well 没有保存 C 栈/恢复执行位置的能力。

因此：

- 不能从 handler 中间切走；
- 不能把 handler 迁移到另一个 worker；
- 不能实现真正的“每请求虚拟线程”。

要做抢占，需要：

```text
Windows Fiber
Linux ucontext
或自研栈切换
+ safepoint
+ panic/作用域展开
```

### 5.5 阻塞语义不同

Well 当前 handler 中：

- 文件 I/O；
- MySQL 同步调用；
- HTTP/WS 部分阻塞操作（HTTP 已改为任务派发，listener 不再等 handler）；

都会阻塞 worker。

Java 虚拟线程会在阻塞点让出 carrier。

这是 Well 与虚拟线程最大的运行时差距之一。

---

## 6. 对 Well 的意义

### 适合 Well 的场景

- 事件驱动；
- 按 oid 分区；
- 同 oid 需要严格顺序；
- 短 handler；
- 不希望用户管理锁；
- 需要确定性 FIFO。

### 不适合 Well 当前模型的场景

- handler 内大量阻塞 I/O；
- 需要百万级并发任务；
- 需要任意任务挂起/恢复；
- 需要长任务公平调度；
- 需要每请求独立栈。

---

## 7. 未来路线

### P0：HTTP/WS 接入 oid 调度器

- HTTP：已完成。listener 只负责解析/收发；handler 作为 task 入 oid 调度器；`Http.respond` 线程安全（首个生效）；同 oid 单 runner。
- WS：已完成。listener 只负责 accept，每连接一个 reader 线程负责握手/收帧；open/msg/close handler 作为 task 入 oid 调度器；`Ws.send/close` 线程安全（按 clientId 查表）。
- MySQL：待做。当前是同步调用，会阻塞 worker。

### P1：调度指标

- worker 利用率；
- steal 次数；
- local deque 长度；
- ready/injector 长度；
- handler 执行时间；
- 长 handler 告警。

### P2：per-worker 队列锁

- 降低 `g_sched_mutex` 竞争；
- local deque 独立锁；
- injector/qmap 分锁。

### P3：阻塞 I/O 处理

- 不能让同步 I/O 长期占 worker；
- 可选：
  - 专用 I/O 线程；
  - 异步 I/O；
  - 协程化。

### P4：抢占 / fiber（可选）

需要：

- fiber/ucontext；
- safepoint；
- reduction；
- panic/作用域展开；
- GC/exec_ref 协调。

这是独立项目，不建议与当前调度器改造混合。

---

## 8. 一句话总结

> **Well 当前是“oid 事件队列 + 固定 OS worker + work stealing + run-to-completion handler”；Java 虚拟线程是“JVM 调度、可挂起恢复、阻塞时让出 carrier 的用户态线程”。两者最根本的区别是：Well 的 handler 没有 continuation，不能暂停/迁移；虚拟线程有 continuation，可以在阻塞点挂起和恢复。**
