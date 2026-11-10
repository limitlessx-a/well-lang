# Well v1 → v2 修改意见（改动清单 / 坑 / 收益 / 难度）

> **基线**：Well v1（现状）= 原子引用计数（RC）+ 帧日志 + 对象池 + 按 oid 的 TTL 整组强拆 + `String` RC（字面量驻留）。
> **目标**：Well v2 = **v2-R**：oid 对象去 RC，改为 **per-oid arena + oid 组级回收 + 组内标记**；保留 `exec_ref[oid]` pin；GC 无 STW；**String 保留 RC**；同时补齐并发、字符串与可观测性改造。
> **口径**：收益数字来自 `well-v2-性能评估.md` / `well-v2-性能对比-多语言.md` 的成本模型（vs v1，±30–50%），不是 benchmark；难度/工作量为**单个熟悉 C 运行时 + 编译器后端的工程师**的粗略估算。落地必须按性能评估 §9 做 v1/v2 A/B。
> **优先级**：P0 = 不做会 UAF/正确性失败；P1 = 决定 v2 收益能否兑现；P2 = 锦上添花/后续版本。
> **配套**：`well-v2-性能评估.md`（成本模型/验收基准）、`well-v2-语言规范.md`（§6.4 GC 语义）、`well-v2-性能对比-多语言.md`（横向定位）。

---

## 0. 一页结论

| 维度 | v1 现状 | v2 目标 | 主要改动 | 预期提升 |
| --- | --- | --- | --- | --- |
| 对象回收 | 原子 RC + 帧日志 + TTL 强拆 | per-oid arena + 组回收 + 组内标记 | A1–A6 | 对象生命周期 **4–10×**；数据密集 handler **1.8–2.8×** |
| 对象头 | 56B | 40–48B | A1 | 对象块 **-14%~-29%**；常驻 **-8%~-20%** |
| 分配器 | 全局池锁 + registry | 组内 bump + 批量归还 | A2 | `new` 35–70ns → **6–20ns**；4 worker 分配 **2–3.5×** |
| 回收触发 | TTL 60s 批 64 | 空闲即触发 + 水位/批量 | A6 | 峰值 RSS **1.05–1.2× v1**（不再线性涨） |
| 正确性 | RC 兜底 | pin/epoch/generation + 深冻结 | B1–B6 | 不做则 UAF；做了不直接提速 |
| 并发/调度 | 全局 `g_sched_mutex`/`g_act_lock` | 分片/每 worker 队列 | C1–C2 | 多 worker **+20%~+80%** |
| HTTP | 每连接 reader 线程 + 跨线程 handoff | 事件驱动/批量 | C3 | 高并发吞吐 **2–5×**（最难，可后置） |
| 字符串 | 80B 头 + 每操作 malloc | 视图/SSO/builder/静态字面量 | E1–E6 | 解析器 **3–4×**；HTTP e2e **+2%~+20%** |
| 构建 | Windows 默认 TCC | gcc/clang -O2 + LTO | C4 | 热路径 **10–30%** |
| **总体** | — | — | 核心 A–B + D | 数据服务 **1.8–2.8× CPU**、内存 **-8%~-20%**、HTTP 单请求 **1.02–1.08×**；并发需 C 类改造 |

---

## 1. 评估口径

### 1.1 难度/工作量标尺

| 标度 | 含义 | 量级 |
| --- | --- | --- |
| ★ | 局部改动，风险低 | 1–3 天 |
| ★★ | 单模块，需测试 | 3–10 天 |
| ★★★ | 跨运行时/编译器，需设计评审 | 1–3 周 |
| ★★★★ | 架构级，需分阶段 | 1–3 个月 |

### 1.2 收益标尺（相对 v1）

- **直接收益**：CPU / 内存 / 延迟 / 吞吐的可测改善；
- **间接收益**：正确性前提、可观测性、为后续优化铺路（可能短期无提速甚至略降）；
- 所有“提升”都是**该改动单独生效**的估算；组合后不是简单相加（GC 摊销、锁竞争、内存峰值会互相影响）。

### 1.3 依赖关系（简化）

```text
B1–B6（正确性/pin/事件/registry）──前置──┐
A1 对象头 ──> A2 arena ──> A3 去 RC 生成码 ──> A4 组回收 ──> A5 组内标记 ──> A6 触发/GC 线程
                                                    │
C1 exec_ref 分片 ──> C2 调度器改造 ──> C3 HTTP 事件驱动（可选/后续）
D1 codegen 清理（随 A3）──> D2 静态字面量/构建优化
E1–E6 字符串（可与 A 并行，但 E1 依赖语义决策）
F1–F4 测试/观测（贯穿）
```

---

## 2. A. 内存与回收（v2-R 核心）

| 编号 | 修改项 | 如何改（关键点） | 坑 | 提升（vs v1） | 难度/工作量 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- |
| **A1** | 对象头重构 | `WellObj` 去 `rc`、去 `reg_next/reg_prev`、去 `alloc_size`（改为单 `next` + flags 里的 mark 位 + size class 存 chunk/空闲链）；56B → **40–48B**；`flags` 加 `MARK`（复用空闲位） | ① 全部 `sizeof`/偏移/元数据/序列化假设要复核；② Registry 若保留则不能去链，得先定 A2 形态；③ TCC/对齐/二进制不兼容；④ 大对象仍需大小记录 | 对象块 **-14%~-29%**；小 VO -25%（arena-native）；常驻 -8%~-20% | ★★ / 3–5 天 | M1 |
| **A2** | per-oid arena 分配器 | 每个 oid 一个 arena：chunk 1–4KB（大组可放大），16B size class，bump 指针 + 组内空闲链；`ctx` 缓存 arena；大对象（>4KB）走 malloc；组元数据池化 | ① 短命小 oid（HTTP 每请求）chunk 浪费/组元数据开销；② 长寿命对象钉住 chunk；③ `Cache.set` 不能搬对象（保持指针稳定）；④ event oid 在 emit 时变化，登记需特殊处理；⑤ oid=0 装配组单独处理 | `new` **35–70ns → 6–20ns**；4 worker 分配 **2–3.5×**；生命周期 **4–10×** | ★★★ / 2–3 周 | M1 |
| **A3** | 删除 oid 对象的 RC 生成码 | codegen 不再生成 `well_retain/well_release/well_track/well_ctx_replace`（String 仍保留）；帧日志只留 pin 栈；`_take` 变体只用于 String/临时语义；destroyer 改为“清指针 + release String” | ① 漏删/漏改会悬垂或泄漏；② `return`/实参/`take`/owned 语义要重审；③ 事件 payload 的 release 路径（现在靠归零 unpin）必须改成显式 unpin；④ static/表初始化/序列化解码路径；⑤ 生成码体积/调试 | 引用字段写 **-40%~-55%**；帧清理 **-75%~-90%**；数据 handler **1.8–2.8×** | ★★★ / 2–3 周（与 A2 联动） | M1 |
| **A4** | 组级回收 | 候选 oid（无任务、`exec_ref==0`、无 Cache 行）→ 摘 act/registry/cache 元数据 → 锁外释放 String 字段 → 整组归还 arena；沿用 `well_oid_take_objects`/`well_force_free_objects` 模式 | ① 候选判定与并发 pin 的竞态（需 epoch，B5）；② 组部分存活（Cache 行）不能整组释放；③ 锁序 act→sched→registry→cache 固定；④ panic/shutdown 路径；⑤ oid 复用 ABA | TTL 4–9µs/100 对象 → **1–5ns/对象批量**；GC CPU 1–5% | ★★★ / 1–2 周 | M1 |
| **A5** | 组内标记（兜底） | 仅当组仍被 Cache/Model 行或外部 pin 引用时：以本组 Cache 行/外部 pin 为根，**只遍历本组对象**；mark 位 + 预分配 worklist；未标记对象清扫到组内空闲链；组清空后 A4 整块归还 | ① 事件 payload 深冻结（B3）否则图会变；② 嵌套容器递归（规范 §6.4.10）；③ 大组必须切片（10k 对象 0.15–0.4ms；1M 对象 15–40ms）；④ 标记栈不能分配/panic；⑤ 部分存活组的 chunk 碎片 | 允许“保留 Cache 数据 + 回收临时对象”；P99 可控；内存不再等 TTL | ★★★ / 1–2 周 | M2 |
| **A6** | 触发与 GC 线程 | 任务完成且队列空/`exec_ref==0` → 立即候选（不等 TTL）；组内分配超水位（8–32MB）→ 任务边界做组内 GC；全局待回收字节 16–32MB → 唤醒 **1 个 GC 线程**；0.5–1ms 切片；`WELL_GC_DEBUG` | ① 触发风暴（每 oid 一次全堆是灾难）；② GC 与 worker 抢 act/registry/pool 锁；③ 调度器隔离（quarantine）与 `begin_cleanup` 复用；④ 大组 GC 不能一次卡死；⑤ 安静系统里 GC 不跑导致 RSS 不降（旧问题） | 峰值 RSS **1.05–1.2× v1**；GC CPU 常态 1–5%（最坏 15%） | ★★ / 1 周 | M1–M2 |

---

## 3. B. 正确性 / pin / 事件（P0，不做会 UAF）

| 编号 | 修改项 | 如何改 | 坑 | 提升 | 难度 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- |
| **B1** | 任务执行 pin | Timer/HTTP/WS/`schedule_task` 在 `dispatch_task` 前后 `well_oid_pin/unpin`（现在只 `ctx_init`，不 pin） | panic/提前 return 必须配对；嵌套 pin 计数 | 正确性（当前是隐藏 UAF） | ★ / 1–2 天 | M0 |
| **B2** | 事件构造 pin | `new Ev(T)` 构造开始 pin `T`，emit/失败释放；或事件在 emit 前不入 oid 组/由帧持有 | 构造期字段写、emit 改 oid、异常路径 | 正确性 | ★ / 2–3 天 | M0 |
| **B3** | payload 深冻结 | 事件构造完成后递归冻结携带的 `Array/Map/Linked/Bytes`；或 dispatch 期统一只读 | ① 现有 FROZEN 只冻结 event 对象；② 自投递（T==ctx.oid）时容器仍可写；③ 深冻结成本 O(payload) | 正确性（无写屏障的前提） | ★★ / 3–5 天 | M0 |
| **B4** | registry / 事件 oid 修复 | String 不入 registry（保持）；`new Ev(0)` 在 emit 改 oid 时迁移登记或单独事件链；oid=0 装配组与事件区分；group list 用 arena 内部链 | 分片键失效、误摘、泄漏；与 A2 接口 | 正确性；GC 才能遍历 | ★★ / 3–5 天 | M0 |
| **B5** | epoch / generation | 组状态带 epoch；pin 与回收判决用 CAS/epoch 握手；oid 复用时 generation++；quarantine（1s 或 1 个 GC 周期） | pin-after-snapshot；旧 unpin 污染新组；ABA | 正确性 | ★★★ / 1 周 | M0–M1 |
| **B6** | 借用/pin 纪律 + ctx 去重 | `Model.get/Cache.get`（带 ctx）pin 源 oid；同 handler 重复 pin 同 oid 只做一次全局锁；禁止无 ctx 的 Model API 在 handler 外裸用；模块持对象必须 pin | ① 现有 `well_model_get` 无 ctx 版不 pin；② `well_ctx_pin` 重复 pin 仍加锁；③ 模块 C 指针不可见 | 性能 + 正确性（pin 去重省 N×2 act 锁） | ★★ / 2–3 天 | M1 |

---

## 4. C. 并发 / 调度 / HTTP（收益大但难）

| 编号 | 修改项 | 如何改 | 坑 | 提升 | 难度 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- |
| **C1** | `exec_ref` 分片 | 1024 桶哈希改为按 oid 分片的原子计数（或 per-oid 原子 + 采样过期）；pin/unpin/touch 不再共用一把全局自旋锁 | ① 与 TTL 判决的原子性；② 内存/缓存行；③ 跨平台原子/对齐 | Cache/事件密集多 worker **+10%~30%** | ★★★ / 1–2 周 | M2 |
| **C2** | 调度器 per-worker 队列 | 去掉每次事件 2–3 对全局 `g_sched_mutex`：每 worker 本地队列 + 有界批量偷取 + 批量唤醒；清理单飞单独锁 | ① 丢失唤醒（仓库已踩过）；② 工作窃取公平性；③ 关闭/重启时序；④ 与 GC quarantine 交互 | 4 worker HTTP 从 **0.5–0.8×** 到 **1.2–1.8×**；事件吞吐 +20%~+60% | ★★★★ / 3–5 周 | M3 |
| **C3** | HTTP 事件驱动 | 把“listener + 每连接 reader 线程”改成 epoll/kqueue/IOCP 事件循环 + 连接状态机；请求/响应批处理；保留现有 handler 语义 | ① Windows/POSIX 两套；② 超时/504/背压；③ WS 复用；④ 与 worker 线程模型整合 | 高并发吞吐 **2–5×**；连接上限/内存大幅改善 | ★★★★ / 6–10 周 | M4（可独立） |
| **C4** | 构建与内联 | 生产用 gcc/clang `-O2`（+`-flto`），不依赖 TCC；热路径函数 `static inline`/LTO；保持 `-fwrapv` | ① Windows 部署需带 gcc；② LTO 构建时间/兼容；③ TCC 单测基线要同步 | 热路径 **10–30%** | ★ / 1–3 天 | M1 |

---

## 5. D. 编译器 / 构建 / 诊断

| 编号 | 修改项 | 如何改 | 坑 | 提升 | 难度 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- |
| **D1** | codegen 清理 | 随 A3 删除 oid 对象的 retain/release/track；保留 String RC 的 `_take`；简化帧日志为 pin 栈；更新 `well_ctx_*` 接口 | 漏删、return/实参/owned 语义、测试期望（rc_debug=0） | 生成码 -5%~-10% 体积；配合 A3 的 CPU 收益 | ★★★ / 2–3 周 | M1 |
| **D2** | 静态字面量 | 编译期按内容去重；生成静态 `WellString`（IMMORTAL）或 ID 表；引用点 O(1)，删除运行时线性驻留查找 | ① 跨文件去重；② 枚举 String；③ 驻留表/初始化时序；④ `slice`/`replace` 共享原串 | 字面量求值 O(n)→O(1)；文本路径 1–5% | ★★ / 3–5 天 | M2 |
| **D3** | GC/内存诊断 | `WELL_GC_DEBUG` 输出周期/对象/字节/耗时/存活组；`well_live_count` 语义改为“活对象数/组数”；帧/arena/组统计 | 旧 `WELL_RC_DEBUG` 测试迁移；计数开销 | 可观测性（无直接提速） | ★★ / 3–5 天 | M1 |
| **D4** | 测试迁移 | 188 个 .we + C 测试中 `rc_debug=0` 改为“GC 后 live 对象/组为 0”；新增 GC 压力、panic、oid 复用、shutdown 用例 | 断言时机（GC 异步）；flaky | 质量保障 | ★★ / 1 周 | M1–M2 |
| **D5** | 规范同步 | 语言规范 §6.4、对象生命周期、pin 规则、`String` 例外、环境变量；删 RC 语义 | 文档与实现漂移 | — | ★ / 2–3 天 | 贯穿 |

---

## 6. E. 字符串（可与 A 并行；HTTP/解析的关键）

| 编号 | 修改项 | 如何改 | 坑 | 提升 | 难度 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- |
| **E1** | zero-copy String 视图 | 二选一：① 新增 `StringView`（ptr+len，借用缓冲区）；② 把 `String` 改成“buffer + offset/len”的 view（一个 buffer 一份 RC，切片只增引用）。解析器从请求/JSON 缓冲区切片；**copy-on-store** | ① 语言语义变更（null/相等/生命周期）；② 父缓冲区生命周期/RC；③ 与 v2 GC/arena 的交互；④ 现有 API/测试面大 | Well 解析器 **9µs → 2–3µs（3–4×）**；HTTP e2e **+10%~20%**（若用 Well 解析）；server 字段零拷贝 **+2%~5%** | ★★★★ / 2–4 周 | M2–M3 |
| **E2** | 短字符串内联（SSO） | 头内联 ≤15–22 字节；短串不 malloc；长串走堆/RC | 头大小/对齐、切片与 SSO 交互、哈希 | 字符串分配 **-30%~-50%**；解析 +20%~30% | ★★★ / 1 周 | M2 |
| **E3** | 惰性 hash | 只在 Map key/相等比较时算 hash；新增 `hash_valid` 位 | 哈希一致性、Map 重排 | 每个字符串省 5–20ns | ★ / 2–3 天 | M2 |
| **E4** | StringBuilder / Bytes 构建 | 新增 `StringBuilder`（或强化 Bytes 作为构建器）；`Json.stringify`、响应、日志改为 builder 一次 toString | 与 String RC/视图的所有权；扩容策略 | 循环拼接 O(n²)→O(n)；10–50×（拼接密集） | ★★ / 1 周 | M2 |
| **E5** | String RC 分片 | 所有 String 的 oid=0 都落 registry 分片 0 → 按 buffer 地址分片/线程缓存；或独立字符串堆 | 释放热点、原子成本、IMMORTAL 字面量 | 多 worker 文本负载 +10%~30% | ★★ / 2–3 天 | M2 |
| **E6** | 解析器专用优化 | header view + 大小写不敏感比较（不做 toLower）；trim 调 offset；JSON 字段名 view、数字快速解析；响应序列化直接写 builder | 语义/兼容、错误处理、编码 | 解析路径 **2–3×**；内存分配 -50% | ★★★ / 1–2 周 | M3 |

---

## 7. F. 观测 / 验证（贯穿）

| 编号 | 修改项 | 如何做 | 坑 | 收益 | 难度 | 阶段 |
| --- | --- | --- | --- | --- | --- | --- |
| **F1** | 分阶段计时 | HTTP 模块加 read/parse/字段创建/派发/handler/respond/write 计时；统计每请求 malloc 数、上下文切换、锁等待 | 计时代价；多 worker 聚合 | 定位真正瓶颈（别再猜字符串/GC） | ★★ / 3–5 天 | M0–M1 |
| **F2** | ASan/Valgrind + 压力 | 全量用例 + GC 压力 + oid 复用 + panic + shutdown mid-GC；32 线程 pin/sweep | 误报、性能开销 | 0 UAF/double-free 红线 | ★★ / 持续 1 周+ | M1–M3 |
| **F3** | A/B 基准 | v1/v2 同机同负载：微基准（分配/引用写/帧清理/组回收）、HTTP、批处理、8h 长稳 | 机器噪声；预热；TCC/gcc 差异 | 验收依据 | ★★ / 1–2 周 | M1–M3 |
| **F4** | 内存长稳 | 8h RSS 斜率 ≤1MB/h；峰值 ≤1.2×；GC 周期计数；无单调增长 | 监控成本 | 防止“性能换 OOM” | ★★ / 持续 | M2–M3 |

---

## 8. 实施路线（建议）

| 阶段 | 内容 | 工期（单人） | 预期状态 |
| --- | --- | --- | --- |
| **M0 正确性** | B1–B5 + F1 + 基础测试 | 1–2 周 | 语义/悬垂风险补齐；性能基本不变 |
| **M1 核心 v2-R** | A1–A4、A6、D1/D3/D4、C4 | 4–6 周 | 数据 handler 1.5–2.2×；内存 -10%；HTTP ≈1.00–1.02× |
| **M2 组内标记/字符串** | A5、D2、E2–E5、C1（可并行） | 3–5 周 | 活跃 oid 内存可控；P99 稳定；文本路径明显改善 |
| **M3 并发/解析器** | C2、E1/E6、F2/F3 完整验收 | 4–6 周 | 多 worker/HTTP 明显改善；文本 2–3× |
| **M4 HTTP 架构（可选）** | C3（事件驱动） | 6–10 周 | 高并发吞吐 2–5×，达到“可对外”水平 |

> **核心 v2-R（M0–M2）单人约 8–12 人周**；含调度器改造（M3）约 14–18 人周；含 HTTP 事件驱动（M4）再 +6–10 周。多人并行时按模块拆分，但 A1–A3 与 B5 强耦合，建议同一人/同一小组负责。

---

## 9. 最大的坑（按危险度）

1. **B1/B2（任务/构造 pin 缺失）**：当前 `dispatch_task` 不 pin、`new Ev(T)` 构造窗口不 pin——这是 v2 的**第一优先级**，否则 GC 一跑就 UAF。
2. **B3（事件 payload 未深冻结）**：会同时破坏“payload pin 快照”和“无写屏障”两个前提。
3. **A3（去 RC 不彻底）**：漏掉 return/实参/事件释放/static/序列化任一路径，都会悬垂或泄漏。
4. **A2（arena 按 oid）与短命 oid**：HTTP 每请求 oid + 每 oid chunk/元数据，若不池化/批量，GC 开销和内存会反噬；**必须配 A6 的 idle 触发与组元数据池化**。
5. **A4/A5（部分存活组）**：Cache 行保留时不能整组释放；若强行整组回收会丢业务数据，必须组内标记。
6. **A6（触发）**：只按 TTL 60s 触发 → RSS 线性涨（30k req/s × 1KB × 60s ≈ 1.8GB）；每 oid 全堆 mark → GC CPU 1–4 秒/秒。
7. **B5（epoch/ABA）**：pin-after-snapshot 与 oid 复用是老 GC 的经典坑，不做会随机 UAF。
8. **D4（测试断言）**：`rc_debug=0` 在 v2 不再等价于“无泄漏”，要改成 GC 后统计，否则测试要么 flaky 要么漏检。
9. **E1（String 视图）**：语言语义/所有权改动，牵一发动全身；建议先做 `StringView` API 或 SSO，别一上来改 `String` 表示。
10. **C2/C3（调度/HTTP）**：收益最大但风险最高，建议独立分支、灰度；不要把 v2 GC 和 HTTP 重写绑在一个 PR。

---

## 10. 不建议做的修改

| 不建议 | 原因 |
| --- | --- |
| 去掉 String RC（把 String 纳入 GC） | 跨 oid 共享、事件字段、模块裸指针都需要它；要做得引入全局字符串堆 + 精确根/句柄，成本远大于收益 |
| 全堆 mark-sweep 作为主回收 | O(活堆) 成本 + 触发风暴；v2-R 的组回收/组内标记已够 |
| 只删 RC、不改分配器 | `new` 仍 35–70ns，收益只有 1.4–1.7×，且内存风险不变 |
| 只按 TTL 触发 GC | 高吞吐请求型负载峰值不可控 |
| 为字符串做“无上限驻留” | 用户输入会撑爆内存；必须 LRU/上限 |
| 先改 HTTP 再改 GC | 两件事的瓶颈不同；先做正确性 + 对象生命周期，HTTP 架构单独排期 |

---

## 11. 验收指标（摘要，详见性能评估 §9）

| 指标 | 目标 |
| --- | --- |
| 对象生命周期（new+回收摊销） | ≤20ns（v1 80–120ns） |
| 数据密集 handler CPU | ≥1.8× v1 |
| 1 万 oid × 100 对象组回收 | ≤2µs/oid；GC CPU ≤2% |
| 8h 长稳 RSS 斜率 | ≤1MB/h、无单调增长 |
| 100MB/s churn 60s 峰值 RSS | ≤1.2× v1 |
| HTTP 最小 handler | p50 不劣化（±2%） |
| HTTP CRUD | p50 -3%~-8%；p99 ≤1.05×；RSS ≤1.2× |
| 4 worker 分配吞吐 | ≥2× |
| 文本解析（若改 String） | ≥2×；目标 3–4× |
| 正确性 | ASan 0 UAF/double-free；pin/oid 复用/panic 负例全部安全 |

---

## 12. 一句话

> **v1 → v2 的核心不是“把 RC 换成标记清除”，而是“把对象生命周期从每对象计数改成 oid 组归属 + arena 批量回收”；收益 60–75% 来自 arena/批量释放，25–40% 来自去 RC。先补 B 类正确性（否则 UAF），再做 A 类（拿收益），最后按需做 C 类（并发/HTTP）和 E 类（字符串）——顺序错了，性能没拿到，稳定性先崩。**
