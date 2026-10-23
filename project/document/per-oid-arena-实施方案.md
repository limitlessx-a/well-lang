# per-oid arena 分配器实施方案（计划稿）

> 状态：计划（待评审/实施）。日期：2026-10-23。关联：`core/wellrt.c` 现有 size-class 池与 RC、`core/well_model.c` TTL 回收、`document/核心代码审计.md` 与 `核心代码审计-第二轮.md`。
> 术语：**per-oid arena** = 按 oid 分组预分配大块（chunk/slab），组内对象用 bump 指针快速切分；oid 组整体回收时整块归还，而不是逐对象 free。它是**分配器方案**，不改变语言语义（RC/oid/TTL/写检查/事件 pin 全部不变）。

## 1. 背景与当前分配路径

当前对象的分配/回收（`runtime/core/wellrt.c`）：

1. `well_new(size, type_id, oid)`（`:391-412`）：`size ≤ 4096` 走 size-class 池 `pool_take`（`POOL_CLASSES=257`，16B 对齐，按 class 整块复用）；否则 `calloc`。对象头记录 `alloc_size`，并 `well_registry_add(o)`（`well_registry_add:59`，256 分片 spinlock）。
2. RC 归零：`well_release`（`:496`）→ 析构字段 → `well_obj_free`（`:159`）→ `pool_return`（小对象回池）或 `free`。
3. oid 组回收（TTL，`well_model.c:216` 起；`WELL_MODEL_TTL_MS` 默认 60s、`WELL_MODEL_TTL_BATCH` 分批 64/上限 256）：`well_oid_take_objects`（`:1655`，registry 分片摘链）→ 锁外 `well_force_free_objects`（`:1660`）→ 每个对象 `force_free_obj`（`:1636`，清容器/字段、`live_dec`、`well_obj_free`）。

现方案的痛点（与审计相关）：

- **逐对象归还**：oid 组回收时每个对象一次 `pool_return`/`free` + live 计数/registry 操作；组越大越慢（B3/B22 已缓解，但仍是逐对象）。
- **池锁竞争**：`pool_take/pool_return` 是全局池锁（B7）；高频分配/释放的 worker 会争用。
- **碎片与 size-class 浪费**：class 向上取整（`alloc_size`），小对象占比高时浪费明显；`WELL_POOL_MAX_MB` 只控制保留量。
- **清理路径仍需遍历每个对象**：`force_free_obj` 必须逐对象清引用/容器（语义所需），但**内存归还**可以批量化。

## 2. 目标与非目标

**目标**

1. oid 组整体回收时，**一次**归还该 oid 的所有 arena chunk（O(#chunks)，不是 O(#objects)）。
2. 组内分配为 **bump**：一次指针加法 + 边界检查，无池锁（同 oid 单 runner 前提下）。
3. 降低小对象分配的 size-class 取整浪费（同一 chunk 内按实际大小切分，仅 8/16B 对齐）。
4. 与现有 RC/TTL/exec_ref/pin/Cache/Store 语义完全兼容；默认行为可开关、可回退。
5. 可观测：统计（chunk 数/字节、浪费、fallback 次数、回收耗时）与 env 可调。

**非目标**

- 不改语言语义：不引入 GC、不改变 RC 时机、不改变 oid 写检查、不改变事件 pin/TTL 判据。
- 不把 `String`（含字面量/动态串）放进 oid arena（见 §3.2）。
- 不替换 `well_registry_*`（oid 枚举/回收仍需 registry；arena 只替换**内存来源**）。
- 不为跨 oid 共享的 immutable/共享对象提供 arena。

## 3. 语义边界（硬约束，先定后写代码）

### 3.1 对象分类矩阵

| 对象 | oid 归属 | 分配器 |
|---|---|---|
| struct / event（`type_id` 为用户类型） | `ctx.oid`（event 可能为 emit 分配的 target oid） | **arena(oid)** |
| Array / Map / Linked（容器） | `ctx.oid`（构造时 oid） | **arena(oid)** |
| Bytes | `ctx.oid` | **arena(oid)** |
| 装配期/static 对象（`oid == 0`） | 0 | **arena(0)（永不回收）** 或现行池（见 §5.9） |
| `String`（字面量、拼接/子串/split/format 结果） | 无 oid 语义，可跨 oid 共享/传递 | **fallback**（现有 `string_alloc_n`/池/malloc；immortal 字面量表保持 COW 表） |
| 大对象（`size > WELL_ARENA_MAX_OBJ`，默认 4096） | 任意 | **fallback**（`calloc/free`，与现行 `>POOL_MAX_SIZE` 路径一致） |
| 显式 oid 创建（Store decode、`new E(oid)`、JSON/Serialize 解码） | 目标 oid | **arena(目标 oid)**（跨线程见 §5.7） |

判定原则：**有 oid 且会被 oid 组整体回收**的对象进 arena；**可跨 oid 共享/无 oid** 的对象（String）与大对象走 fallback。

### 3.2 为什么 String 不进 arena

`String` 可在不同 oid 之间传递（事件字段、Model 值、内建参数），且字面量被全局 intern（COW 表 + IMMORTAL）。若 String 放进某 oid 的 chunk，会随该 oid 回收而失效，而其它 oid 仍可能持有引用 → 悬空。因此 String 保持现有分配路径（`string_alloc_n:885`、`lits_insert:933`、池/malloc），只把 **oid 拥有** 的聚合对象放进 arena。

### 3.3 RC 与 arena 的关系

- RC **语义不变**：`well_release` 在 rc→0 时仍执行析构（释放字段/容器内引用、`live_dec`），但 arena 对象**不立即归还内存**，只在对象头标记 `ARENA`（可选 `DESTROYED`）。
- **禁止 double destructor**：arena 对象析构后置 `WELL_FLAG_DESTROYED`；`force_free_obj` 对已析构对象只做 `live_dec`/registry 摘除，不重复清字段。
- 环：仍不靠 RC 回收，随 oid 组回收整块释放（与现语义一致）。
- `well_force_free_objects`（`:1660`）需知道对象是否 arena：arena 对象跳过 `well_obj_free`，仅析构；所有对象处理完后由回收路径对每个 oid 调 arena 回收。

### 3.4 跨 oid 引用、pin、Cache/Store、shutdown

- event payload 可携带其它 oid 对象（`event_pin_visit` 全引用 pin）。arena 回收沿用现有判据：`exec_ref==0` + 调度器空闲 + 无 pin（T-15 修复后 pin 覆盖全部非 String 引用）；回收前必须确认 pin 归零，否则延后（与 TTL 现有行为一致）。
- Cache 行在该 oid 回收时按现行逻辑丢弃（`well_cache_discard_oid`），因此 Cache 持有的对象不会在 arena 释放后存活。
- MemoryStore 字节行不依赖对象内存；decode 分配的目标 oid 对象进对应 arena；Store 回读可能创建"更早 oid"的对象——以 decode 参数 oid 为准。
- `well_shutdown`：先 quiesce 再回收全部 arena（含 oid0 的永久 arena 仅释放块，不做逐一析构或按现行顺序）。

## 4. 方案总览

```
well_new(size, type_id, oid)
  ├─ 属于 arena 类别 && oid != 0 && size ≤ max_obj
  │     └─ arena_alloc(oid, size)            // bump；chunk 满则新 chunk
  ├─ oid == 0（装配期/static）
  │     └─ arena0_alloc(size) 或 fallback     // 见 §5.9
  └─ 其它（String / 大对象 / fallback budget 用尽）
        └─ 现行 size-class 池 / calloc
well_release(obj)                                   // 不变
  └─ rc==0 → destructor → arena 对象不 free，置 DESTROYED
oid 回收（TTL / shutdown）                           // well_model.c:216 起
  ├─ take registry（不变）
  ├─ 逐对象 destructor（不变；arena 对象跳过 obj_free）
  └─ arena_free_all(oid)                            // 整块 free：O(#chunks)
```

## 5. 详细设计

### 5.1 数据结构

```c
typedef struct WellArenaChunk {
    struct WellArenaChunk *next;
    size_t size;          /* chunk 总字节 */
    size_t used;          /* bump 偏移 */
    size_t waste;         /* 因对象析构造成的不可用字节（统计） */
    /* 数据紧随其后（inline） */
} WellArenaChunk;

typedef struct WellArena {
    long long oid;
    WellArenaChunk *chunks;   /* 已用块链 */
    WellArenaChunk *spare;    /* 可选：回收时保留 1 块复用 */
    size_t chunk_size;        /* 默认 WELL_ARENA_CHUNK_KB */
    size_t total_bytes;
    size_t obj_count;
    int    frozen;            /* oid0：永不回收 */
    WellLock lock;            /* 跨线程分配时保护 bump（方案 A） */
} WellArena;
```

- **表**：`ArenaTable` = 分片哈希（复用 256 分片思路或与 registry 同 oid 同分片）`oid → WellArena*`；创建于首次 arena 分配，销毁于 oid 回收。
- 与 registry 的关系：registry 仍负责"oid 有哪些对象"；arena 只负责"这些对象的内存在哪"。二者在回收路径汇合（先 take registry，析构对象，再 arena free）。
- 对象头复用现有 `WellObj.flags` 位（新增 `WELL_FLAG_ARENA = 1u<<n`、`WELL_FLAG_DESTROYED`），`alloc_size` 保留实际分配大小（用于统计/兼容）。

### 5.2 分配（bump）

`arena_alloc(oid, size)`：
1. 取（或建）`WellArena`；对齐 `align = 16`（或 `WELL_ARENA_ALIGN`，按 ABI 要求）。
2. 当前 chunk 剩余 ≥ `align_size` → `p = chunk->data + chunk->used; chunk->used += align_size;`
3. 否则分配新 chunk（`max(chunk_size, align_size + header)`），链入，重复 2。
4. 返回 `p`；调用方（`well_new`）清零（只清 `size`，不是整块，保持现在语义）、写对象头、registry add、`live_inc`。
5. **失败**：`malloc` 失败 → 回退 fallback（若允许）或 `well_panic("out of memory (arena)")`。

**bump 并发**：见 §5.7。

### 5.3 释放与回收

- `well_release` 不变；`well_obj_free(obj)` 中若 `obj->flags & WELL_FLAG_ARENA`：**不归还内存**（标记 DESTROYED），只做 `live_dec`；registry 摘除仍由现有 release/回收路径负责。
- `force_free_obj`：arena 对象若未 DESTROYED 则执行析构（容器 `force_clear`、字段清理、`event_unpin_payload`），然后 `live_dec`；不调用 `well_obj_free`。
- `well_force_free_objects(list)`：逐对象同上；返回后由调用方（TTL 清理/`well_shutdown`）对每个 oid 调 `arena_free_all(oid)`。
- `arena_free_all(oid)`：遍历 chunks `free(chunk)`，删除 arena 表项，累计统计；oid0 的 arena 跳过（frozen）。
- **spare 策略**：可选保留 1 个 chunk 到全局 spare 缓存，降低"分配→回收→再分配"的 malloc 频率；需上限（`WELL_ARENA_SPARE_MAX_MB`）。

### 5.4 与对象析构/容器的配合

- 容器 `force_clear` 当前会释放元素/内部数组；arena 对象的"内部数组"（如 Array 的 items、Map 的 slots）**是否也进 arena？** 两种选择：
  - **A（推荐，低风险）**：容器内部动态数组仍走 fallback（`malloc/free`），容器对象本体进 arena。回收时 `force_clear` 正常 `free` 内部数组；容器本体由 chunk 整块回收。内存收益主要在对象头/对象本体，实现改动小。
  - **B（收益更大）**：容器内部数组也按 oid arena 分配，重分配/扩容时旧块标记 waste；回收整块。需处理扩容时的旧块、`get_take` 等所有权路径，风险高。
- 建议 Phase 1–3 采用 A；Phase 4 评估 B（作为可选优化，独立开关）。

### 5.5 内存上限与 fallback

- `WELL_ARENA_MAX_OBJ`（默认 4096）：单对象超过则 fallback（与现 `POOL_MAX_SIZE` 一致）。
- `WELL_ARENA_CHUNK_KB`（默认 64）：chunk 大小；大对象密集场景可调大。
- `WELL_ARENA_PER_OID_MAX_MB`（默认 64）：单 oid arena 上限；超过则**新对象 fallback**（对象头记录 fallback，回收时逐个 free），防止单个热点 oid 无限膨胀。
- `WELL_ARENA_GLOBAL_MAX_MB`（默认 `WELL_POOL_MAX_MB` 或 256）：全局 arena 预算；超出后新 arena 分配 fallback。
- `WELL_ARENA=0/1`（默认 **0 关闭**，Phase 4 后再评估默认开启）：一键回退现有池。
- OOM 策略：arena 分配失败 → 优先 fallback；fallback 也失败 → `well_panic("out of memory")`（保持现状）。

### 5.6 统计与可观测

- `WELL_RC_DEBUG=1` 输出扩展：`arena: oids=N chunks=M bytes=B waste=W fallback=F reclaims=R`。
- 轻量计数器（原子/单 runner 下普通计数）：`arena_alloc_calls`、`arena_chunks`、`arena_bytes`、`arena_waste`、`fallback_calls`、`reclaim_total_us`、`reclaim_objects`。
- 建议增加 `WELL_STATS=1` 每 N 秒打印一次（不进入语言层）。

### 5.7 并发模型（两个选项）

- **方案 A（推荐，先做）**：`WellArena.lock`（spinlock）只保护 bump 指针与 chunk 链；同 oid 对象分配通常由同一 runner，锁几乎无竞争；跨线程（decode/显式 oid）正确。回收需先确保该 oid 无分配者（现有清理前置条件已保证 exec_ref==0/队列空），再在锁内解链、锁外 `free`。
- **方案 B（后续优化）**：thread-local 当前 chunk（`WellTlsKey`）+ 原子替换 chunk；回收需遍历所有线程缓存（复杂），或给 arena 记录"owner 线程 + 借用"。
- **结论**：Phase 1–3 用 A；性能数据表明锁竞争仍高时再评估 B。

### 5.8 与现有池/registry 的关系

- 池保留：String、fallback 对象、大对象、`POOL` 复用路径不变；`well_pool_sweep` 继续工作。
- registry 不变（oid 枚举、take、pin 检查）。
- TTL 清理流程：`well_oid_take_objects` → 析构 → `arena_free_all`；仍需单飞/分批（`WELL_MODEL_TTL_BATCH`），分批的粒度从"对象"变成"对象+chunk 回收"（每 oid 一次）。
- 装配期 oid0：`arena(0)` 标记 frozen，不回收；或继续走现有池（Phase 1 可先保持现状，Phase 3 再迁移，降低风险）。

## 6. 分阶段实施

| 阶段 | 内容 | 产出 | 估算 |
|---|---|---|---|
| **P0 测量** | 加分配/回收/锁竞争统计（`WELL_STATS`），跑 `tools/bench.py` 与 188 用例基线；确认热点（分配次数、清理耗时、池锁） | 基线报告 `tool_ai/audit/arena-baseline.md` | 0.5–1 天 |
| **P1 骨架** | `WellArena/Chunk` + 表 + `arena_alloc/free_all`；对象头 flags；`WELL_ARENA=0` 默认关闭；C 单元测试（alloc/reclaim/对齐/上限/spare） | `well_arena.c/h`；C 测试 | 2–3 天 |
| **P2 接入** | `well_new` 分类分流；`well_release`/`force_free_obj` 跳过 arena 对象 free；TTL/`well_shutdown` 调 `arena_free_all`；String/大对象排除；容器内部数组按 §5.4-A | 默认关闭、可开关运行；188 用例 + C 测试 + TTL/pin 回归 | 2–3 天 |
| **P3 并发与压力** | 跨线程 decode/显式 oid 锁化；多 worker/TTL/环/event pin 压力；`WELL_RC_DEBUG` live=0；修复泄漏/双析构 | 压测报告 + 回归全绿 | 2–3 天 |
| **P4 默认化** | 对比 bench（parser/http/alloc 微基准）；内存/吞吐验收；`WELL_ARENA` 默认开启（保留 0 回退）；文档（IMPLEMENTATION §3.x、README env） | 性能报告 + 文档 + 默认开启 | 1–2 天 |

回滚：`WELL_ARENA=0` 即可完全回到现路径；代码保留 fallback 分支，无需 revert。

## 7. 测试计划

**功能/C 单元测试**（`tests/c/arena_test.c` 等）
1. 小对象 bump 分配/对齐/清零；跨 chunk 边界；单 chunk 放不下的大对象走 fallback。
2. RC 归零→析构→DESTROYED；重复 release 不重复析构；`force_free` 对已析构对象安全。
3. oid 回收：多对象/多 chunk 一次释放；`live` 计数归零；arena 表项删除；重复回收幂等。
4. oid0 frozen 不回收；shutdown 回收全部。
5. 上限：per-oid/global cap 触发 fallback；fallback 对象回收逐个 free。
6. 并发：N 线程对同一/不同 oid 分配；回收与分配互斥；无崩溃/泄漏。
7. 容器 force_clear 后 chunk 回收无 UAF（ASan/LSan 可选）。

**端到端回归**：`python tools\verify.py`（188 `.we` + 18 C）必须全绿；重点 `40_cycle`/`41_cycle_container`/`91_default_ttl_cleanup`/`93_oid0_never_cleaned`/`113_cache_api`/`128_mysql_big_column`/`151_member_chain_leak`/`183/184`/HTTP/WS/MySQL 用例。
**泄漏**：`WELL_RC_DEBUG=1` 下 live=0；长跑循环（1e6 次分配/回收）RSS 曲线平稳。
**性能**：`tools/bench.py --scenario all` 对比基线；单测分配/回收 µs/op；TTL 清理 1e5 对象耗时；HTTP 吞吐 ±5% 内。

## 8. 验收标准

- **正确性**：188/188 `.we` + 18 C 测试通过；所有 TTL/pin/oid0/Cache/Store 回归通过；无 double-free/UAF（C 测试 + 压力 24h 可选）。
- **语义**：RC 时机、oid 写检查、事件 pin、字符串共享行为逐项不变（用现有用例 + 新增对照程序验证）。
- **性能**：TTL 整组回收时间相对基线下降 ≥50%（大组场景，1e4 对象）；分配路径（bump vs 池）不慢于现状；HTTP 吞吐不下降 >5%。
- **内存**：同负载峰值 RSS 增幅 <10%（可调 cap/阈值）；长跑无单调增长。
- **可观测/可回退**：`WELL_ARENA=0` 一键回退；统计输出可核对。

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| RC 早释放但内存滞留 → 峰值内存上升 | 长生命周期 oid 内存膨胀 | per-oid cap + fallback + 建议缩短 TTL/拆子 oid |
| 跨 oid/共享对象误入 arena（如 String） | 悬空/崩溃 | §3.1 白名单 + 断言（debug 下检查 `flags & ARENA` 与 type） |
| double destructor | UAF | DESTROYED 标记 + 单测（重复 release/重复 force） |
| 回收与分配竞态（跨线程显式 oid） | 内存破坏 | 方案 A 锁化 bump；回收前置条件（exec_ref==0/队列空）+ 锁内解链 |
| chunk 内浪费（对象大小不一） | 内存膨胀 | 16B 对齐、chunk 大小可调、统计 waste；Phase 4 评估 size-class chunk |
| 容器内部数组仍走 malloc（方案 A） | 收益打折 | 先交付安全版本；Phase 4 评估方案 B |
| 与 Store/Cache decode 生命周期 | 对象提前释放 | decode 以目标 oid 分配；Cache 行在 oid 回收时丢弃（现状） |
| TTL 批次语义变化 | 延迟/公平性 | 批大小仍按对象数计；chunk 回收在锁外 |
| POSIX/32 位对齐 | 未验证 | CI Linux gcc + 可选 32 位构建；对齐常量可配 |

## 10. 开放问题（需决策）

1. **默认开关**：Phase 4 是否默认开启 `WELL_ARENA`？建议先做 A/B 数据再定。
2. **oid0/static 是否迁入 arena0**：Phase 1 保持现状更稳；若迁入，需保证永不回收且 static 引用释放顺序不变。
3. **容器内部数组**：方案 A（对象本体 arena、数组 malloc）够不够？是否在 Phase 4 做 B？
4. **默认上限值**：per-oid 64MB / global 256MB / chunk 64KB 是否合适（按业务负载调）。
5. **String 是否未来做 per-oid 复制**（把跨 oid 共享串改为"进入某 oid 时复制"）？这会改变性能模型，建议不纳入本计划。

## 11. 与替代方案对比

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 现状（size-class 池 + 逐对象 free） | 已稳定、内存可复用 | 清理 O(#obj)、池锁竞争、class 浪费 | 保留为 fallback |
| **per-oid arena（本方案）** | 回收 O(#chunk)、bump 快、浪费小、可统计 | RC 早释放内存滞留、需处理 String/跨线程 | **采用** |
| per-type arena | 按类型分块，便于 size-class | 回收按 oid，跨类型组仍要逐块找 | 不如按 oid 直接 |
| malloc + free 批处理（只延迟归还） | 改动最小 | 仍逐对象 free，收益有限 | 作为备选 |

## 附录 A：关键代码位置

| 位置 | 说明 |
|---|---|
| `runtime/core/wellrt.c:391-412` | `well_new`：现有分配入口，改造点 |
| `runtime/core/wellrt.c:144-170` | `pool_take`/`well_obj_free`（fallback 路径保留） |
| `runtime/core/wellrt.c:59,81` | registry add/take（回收路径） |
| `runtime/core/wellrt.c:496` | `well_release`（析构时机不变） |
| `runtime/core/wellrt.c:1636-1664` | `force_free_obj`/`well_force_free_objects`（改为跳过 arena free + 触发 chunk 回收） |
| `runtime/core/well_model.c:216-262` | TTL 清理（单飞/分批），在 take+force-free 后调 `arena_free_all` |
| `runtime/core/wellrt.c:885,933` | `string_alloc_n`/`lits_insert`（String 保持 fallback） |
| `runtime/core/wellrt.h`（WellObj 头） | 新增 `WELL_FLAG_ARENA/DESTROYED`，`alloc_size` 语义不变 |

## 附录 B：env 配置一览

| 变量 | 默认 | 说明 |
|---|---|---|
| `WELL_ARENA` | `0`（Phase 4 后评估默认 1） | 0=现路径，1=启用 per-oid arena |
| `WELL_ARENA_CHUNK_KB` | 64 | chunk 大小 |
| `WELL_ARENA_MAX_OBJ` | 4096 | 超过则 fallback |
| `WELL_ARENA_PER_OID_MAX_MB` | 64 | 单 oid 上限，超出 fallback |
| `WELL_ARENA_GLOBAL_MAX_MB` | 256 | 全局 arena 预算 |
| `WELL_ARENA_SPARE_MAX_MB` | 16 | 回收后保留 spare chunk 上限（可选） |
| `WELL_STATS` | 0 | 1=周期性打印统计（不含语言层） |
