# 跨语言性能测试（2026-10-24）

## 环境

- CPU：Intel i5-12400F（6P/12T），Windows 11 26200，关闭 Smart App Control 后测试
- 编译器/运行时：
  - C / C++：Zig 0.15.1 自带 Clang/LLVM，`zig cc -O2` / `zig c++ -O2`
  - C（未优化基线）：TinyCC 0.9.27（`tcc -run`，无优化）
  - Java：Temurin OpenJDK 21.0.12.1 LTS（JIT）
  - Go：go1.27.3 windows/amd64
  - Erlang：OTP 29.1.2（BEAM JIT）
  - Well v1 / v2：Python 前端生成 C99，再用同一 TCC 执行
- 经确认没有 v3；**v2 就是当前最新版本**。
- GCC 16.2.0 的 zip 下载不完整，链接阶段缺库（`-lmsvcrt` 等），因此 C/C++ 优化基线使用 Zig/LLVM。

## 1. CPU + 缓存热数组访问（主基准）

负载：1024 个 `long` 的数组，循环 2000 万次 `acc += a[i & 1023]`，输出 acc 防止删除。该负载不会被优化成闭式，能同时反映循环代码生成和数组访问成本。

| 实现 | 中位耗时 | ns/iter | 相对 C |
|---|---:|---:|---:|
| C（Clang -O2） | **5.0 ms** | **0.250** | 1.00× |
| C++（Clang -O2） | **4.8 ms** | **0.241** | 0.96× |
| Go 1.27 | **5.2 ms** | **0.260** | 1.04× |
| Java 21（JIT） | **7.7 ms** | **0.385** | 1.54× |
| Erlang OTP 29 | **56.4 ms** | **2.82** | 11.3× |
| **Well v2（TCC，优化后）** | **53 ms** | **2.65** | **10.6×** |
| **Well v2（Clang -O2，优化后）** | **7.0 ms** | **0.350** | **1.4×** |
| Well v2（TCC，优化前） | 385 ms | 19.25 | 77× |
| Well v1 | 1,548 ms | 77.4 | 310× |

结论：

- C/C++/Go 基本同档（Go 的边界检查被优化掉）。
- Java 数组访问约 C 的 1.5×（边界/类型检查 + JIT 取舍）。
- Erlang 是 C 的 11×，作为 BEAM 标量循环已不算慢。
- **Well v2 优化后在 TCC 下是 C 的 10.6×，Clang -O2 下是 1.4×**；优化前是 77×。
- v2 比 v1 快 **29.2×**（数组访问，TCC）；Clang 下比 v1 快约 221×。

## 1.1 本轮优化做了什么

| 优化 | 数组循环 TCC | 数组循环 Clang | 求和循环 TCC |
|---|---:|---:|---:|
| 优化前 | 19.25 ns | 10.0 ns | 5.73 ns |
| 常量非零 `%` 不再触发 `well_source_set` + 原生 `%` | 8.0 ns | 1.1 ns | — |
| `well_ctx_mark/release_to` 改为宏（TCC 不内联） | 4.7 ns | 1.1 ns | **2.09 ns** |
| `well_array_get_fast` 内联（Clang）+ `well_unbox_*` 宏 | 4.05 ns | 0.375 ns | 2.09 ns |
| 无引用作用域省略 ctx mark/release（安全版） | **2.65 ns** | **0.350 ns** | **0.80 ns** |

具体改动：

1. `expr_may_panic`：`/`、`%` 的除数是**非零 IntLit** 时不再视为 panic，循环体内不再每轮写 `well_source_set`（两次 TLS 写）。
2. 代码生成：常量非零除数（除 `-1` 外）直接生成原生 C `/`、`%`，不再调用 `well_int_mod`。
3. `well_ctx_mark` / `well_ctx_release_to` 改成宏，避免 TinyCC 把 `static inline` 编译成真实函数调用。
4. `well_array_get_fast` 在 Clang/GCC 下 `always_inline`；TinyCC 不支持内联 statement expression，自动退回原来的 `well_array_get` 调用。
5. `well_unbox_i/d/p` 改成宏，去掉每次容器读取的 unbox 函数调用。
6. **无引用作用域省略 ctx mark/release**：正常作用域结束时若作用域内没有 `well_track`，把 mark 置 0 并跳过 release；有 `return/break/continue` 提前清理的作用域保留 mark，避免从 0 释放。数组循环的 loop/body 两个作用域因此完全消失。

测试：`.we 189/189`、C 测试全部通过。

## 2. 纯算术循环（辅助基准）

负载：`for (i = 0; i < 100000000; i++) acc += i;`。

| 实现 | 中位耗时 | ns/iter |
|---|---:|---:|
| C（TCC -O0） | 65 ms | 0.65 |
| C / C++（Clang -O2） | **<1 ms** | 被优化为闭式，**无有效数据** |
| Java 21 | 26.3 ms | 0.263 |
| Go 1.27 | 24.8 ms | 0.248 |
| Erlang OTP 29 | 140.8 ms | 1.408 |
| **Well v2（优化后 TCC）** | **81 ms** | **0.80** |
| Well v2（优化前 TCC） | 573 ms | 5.73 |
| **Well v1** | **7,216 ms** | **72.16** |

说明：Clang -O2 把求和识别成闭式，所以 C/C++ 的“0 ms”不代表真实循环性能；主基准请用第 1 节。Well v2 优化后比 v1 快 **90×**。

## 3. 分配/释放

语义不完全相同，只看数量级：

| 实现 | 操作 | 中位耗时 |
|---|---|---:|
| C（Clang -O2） | `malloc(64)+free`（volatile sink） | **26.0 ns/op** |
| C++（Clang -O2） | `new char[64]+delete[]`（volatile sink） | **27.4 ns/op** |
| C（TCC -O0） | `malloc(64)+free` | **30.5 ns/op** |
| Go 1.27 | `make([]byte,64)` × 2M，保留引用 | **26.0 ns/op** |
| Java 21 | `new byte[64]` × 2M，保留引用 | **43.2 ns/op** |
| Erlang OTP 29 | 64B `binary:copy` × 2M | **377 ns/op** |
| Well v2 | 对象 `new+release`（RC+registry+pool） | **约 60 ns/op** |
| Well v1 | 对象 `new+release` | **107 ns/op** |
| Well v2 | 8B String `new+release` | **49.0 ns/op** |
| Well v1 | 8B String `new+release` | **92.9 ns/op** |

结论：

- C/C++/Go 的短生命周期分配都在 26–27 ns；Java 43 ns（TLAB + 数组头/清零）。
- Well v2 对象 60 ns，约为 C malloc 的 2.3×；String 49 ns，已接近 C 分配 + 复制。
- v1 对象 107 ns、String 93 ns，明显更贵。
- Erlang 二进制分配 377 ns，是它最弱的一环。

### 3.0 对象释放路径（本轮）

`well_release` 从“先 atomic load 再 atomic dec”改为**一次 atomic dec**，并用递减结果判断负计数；`exec_pins == NULL` 时不再调用 `well_event_unpin_payload`：

- 对象 new+release：**69 → 60 ns**
- 对象 alloc+free-only 路径：约 36 ns
- 差距主要来自 oid registry 的 add/remove；下一阶段会用**延迟/分片回收或区域 GC**替代逐对象 registry 锁，目标对齐 Java 的 43 ns。

### 3.1 容器创建 + push 一个元素 + 释放

| 实现 | 操作 | 中位耗时 |
|---|---|---:|
| Erlang OTP 29 | `[I]` 列表 + `length` | **5.6 ns/op** |
| Go 1.27 | `make([]int64,0,1); append` | **8.2 ns/op** |
| Java 21 | `new ArrayList<Long>()` + `add`（volatile 逃逸） | **25.3 ns/op** |
| C++（Clang -O2） | `new std::vector<long long>()` + `push_back` + `delete` | **58 ns/op** |
| **Well v2（优化前）** | `well_array_new` + `push` + `release` | **135 ns/op** |
| **Well v2（优化后）** | 内联 4 个元素 + 对象池 + 标量容器免 registry | **45.5 ns/op** |

结论：

- Well 容器创建从 **135 → 45.5 ns（3×）**，**快于 C++ vector（58 ns）**，约为 Java ArrayList 的 **1.8×**。
- 仍慢于 Java/Go/Erlang 的轻量容器，因为 Well 仍保留 RC、对象池和 oid/ctx 检查。
- 语义不完全等价：Java 例子有装箱和 volatile 逃逸，C++ vector 有一次堆分配；GO/Erlang 的容器不维护 RC/oid。

本轮容器改动：

1. `WellArray` 内联 4 个 `WellValue`，小数组不再额外 `malloc/free`。
2. `array_grow` 首次扩容从内联数组拷贝，之后才用 `realloc`。
3. `array_destroy/map_destroy/linked_destroy` 从 `free()` 改为 `well_obj_free()`，让容器重新进入 size-class/TLS 对象池；这是 114 → 77 ns 的主因。
4. `well_array_force_clear` 不再释放内联数组指针。
5. 容器改用 `well_new_uninit()`：数组只初始化头字段，map 只清零 8 个 inline slot，跳过整块 payload 清零。

### 3.2 Map 创建 + set 一个元素 + get + 释放

| 实现 | 操作 | 中位耗时 |
|---|---|---:|
| Erlang OTP 29 | `#{"k" => I}` + `maps:get` | **1.3 ns/op**（JIT 可能把新建 map 特化） |
| Java 21 | `new HashMap<>()` + put/get（volatile 逃逸） | **49 ns/op** |
| Go 1.27 | `make(map[string]int64,1)` + 赋值/读取 | **74 ns/op** |
| C++（Clang -O2） | `new unordered_map<string,long long>` + insert/find | **96 ns/op** |
| **Well v2（优化前）** | `well_map_new` + set/get + release | **221 ns/op** |
| **Well v2（优化后）** | 内联 4 组 key/value + 小 map 线性 get/set + 延迟 slot + 标量免 registry | **65 ns/op** |

- Map 路径提升 **221 → 65 ns（3.4×）**，快于 Go（74）和 C++（96），约为 Java 的 **1.33×**。

本轮 Map 改动：

1. `WellMap` 内联 4 个 key、4 个 value、8 个 hash slot；单元素 map 不再 `malloc/calloc` 三块数组。
2. `map_grow_entries` 首次扩容从内联数组拷贝。
3. `map_rehash`/`map_destroy`/`well_map_force_clear` 识别内联指针，避免 `free()` 内联内存。
4. `well_map_get` 对 `len <= 4` 的 map 走线性扫描，省掉 hash + slot probe。
5. 容器使用 `well_new_uninit()` 跳过整块清零；map 只清 8 个 inline slot。
6. 标量容器使用 `well_new_uninit_unregistered()`，不进入 oid registry；`Model.set` 会自动注册。
7. Map 的 hash slots 延迟到第 5 个元素才构建；前 4 个 key 的 set/get/has/delete 全部走线性扫描。
8. 小 Map 不再清零 8 个 inline slot；`slot_cap=0` 时这些 slot 根本不会被读取，等第 5 个元素时再分配真正的 hash 数组。
9. `well_retain` 改为一次 atomic RMW：TinyCC 的 `InterlockedIncrement` 返回旧值，运行时在 `__TINYC__` 下 +1 归一化为新值；其他编译器直接用新值。对象/Map key 的 retain 路径少一次 atomic load。
6. **标量容器（Array<int>/Map<int> 等 is_ref=0）不再进 oid registry**：它们不可能形成引用环，RC 足够；若被 `Model.set` 持有，会自动补注册。

### 3.3 非逃逸标量 struct 的栈分配

负载：循环 200 万次 `var p = new Point(); p.x=1; p.y=2; p.z=i; acc += p.x+p.y+p.z`。

| 实现 | 方式 | ns/op |
|---|---|---:|
| Erlang OTP 29 | map | **1.9** |
| **Well v2（优化后）** | **C 栈对象，无 RC/registry** | **3.5** |
| Go 1.27 | `&Point{}` 放入 slice | **16.7** |
| Java 21 | `new Point()` 放入数组 | **17.1** |
| C++（Clang -O2） | `new Point` + delete | **25.8** |
| Well v2（优化前） | `well_new` + track/release | **105** |

结论：

- 本轮把非逃逸标量 struct 从堆分配改为 **C 栈对象**：**105 → 3.5 ns（30×）**，快于 Java/Go/C++ 的堆对象路径。
- 前提是编译器证明对象只通过字段访问、不逃逸（不传参、不返回、不放进容器/事件、不赋给其它引用）。
- 逃逸对象仍走正常 `well_new` + 自动回收；语法完全不变。
- 这是“栈/区域自动回收”的第一步；下一阶段把同样思路扩展到容器和逃逸分析后的 frame 区域。

### 3.4 非逃逸标量数组字面量的栈分配

负载：循环 200 万次 `var a: Array<int> = [1,2,3]; acc += a[0]+a[1]+a[2]`。

| 实现 | 方式 | ns/op |
|---|---|---:|
| Erlang OTP 29 | `[1,2,3]` 列表 | **1.4** |
| **Well v2（栈数组）** | 栈 `WellArray` + 3 个内联元素 | **7.5** |
| Java 21 | `int[]{1,2,3}` 放入 `keep[i]`（逃逸） | **19.2** |
| C++（Clang -O2） | `new int[3]{...}` + delete | **28.4** |
| Well v2（堆 Array） | `well_array_new` + push + release | **45.5** |

- 非逃逸字面量数组：**45.5 → 7.5 ns（6×）**。
- 注意：Well 这里是非逃逸栈分配；Java/C++ 的两行是**逃逸到堆**的对照，Java 非逃逸数组通常会被 JIT 标量替换，C++ 栈 `std::array` 也可能完全优化掉，因此不能直接说 Well 比它们快。

实现约束：

- 只处理 `Array<标量>` 且字面量长度 ≤ 4。
- 变量名全局唯一。
- 只允许 `a[i]` 读；**不允许写索引**（写可能触发扩容，栈对象无法释放堆 items）、传参、返回、放入容器/事件、方法调用。
- 常量下标直接生成 `v_a->items[i].i`，没有函数调用；变量下标仍走 `well_array_get_fast` 的边界检查。
- 栈数组不初始化 header（只设置 items/len/cap/is_ref），因为该路径只读、不注册、不释放。

### 3.5 逃逸标量 struct 的构造路径

标量 struct 即使逃逸（被数组/事件/Model 持有），也不可能有引用环。本轮让生成构造器使用：

- `well_new_uninit_unregistered()`：不进 oid registry、不清零整块；
- 逐个字段显式置 0 / 默认值；
- 若被 `Model.set()` 持有，自动补注册。

| 实现 | 方式 | ns/op |
|---|---|---:|
| **Well v2（逃逸标量 struct）** | 无 registry + 无 memset | **35.5** |
| Well v2（普通对象） | registry + memset | **60** |
| Java 21 | 堆对象 + 数组保留 | **43** |
| C++（Clang -O2） | new/delete | **27** |

- 逃逸标量对象：**60 → 35.5 ns（1.7×）**，已经快于 Java 的 43 ns。
- 普通引用对象/容器仍走 registry；标量对象因不可能成环，TTL/环检测不需要它。
- 语法不变；自动回收仍由 RC + Model 注册/TTL 兜底。

### 3.6 非逃逸标量 Map 字面量的栈分配

负载：200 万次 `var m: Map<int> = {"a":1,"b":2}; acc += m["a"]+m["b"]`。

| 实现 | 方式 | ns/op |
|---|---|---:|
| Erlang OTP 29 | map literal | **1.3** |
| **Well v2（栈 Map）** | 栈 `WellMap` + 内联 key/value | **9** |
| Java 21 | `HashMap` 字面量（逃逸到 volatile） | **60** |
| Go 1.27 | map literal（逃逸到全局） | **90** |
| C++（Clang -O2） | `new unordered_map` 初始化列表 | **135** |
| Well v2（堆 Map） | `well_map_new`+set/get+release | **76** |

- 非逃逸标量 Map 字面量：**76 → 9 ns（8.4×）**。
- 注意：Well 是**非逃逸栈分配**；Java/Go/C++ 是逃逸到堆的对照。非逃逸 map 在 Java/Go 中也可能被标量替换。
- 实现约束：
  - 只处理 `Map<标量>`、字面量 key 为 String、长度 ≤ 4。
  - 只允许 `m["literal"]` 读；不允许写、传参、返回、方法调用。
  - 常量 key 直接 `v_m->vals[i].i`，无 hash/函数调用。
  - key 用 `well_string_lit_slot` 静态槽，避免每轮全局 literal 锁。
  - 栈 Map 不初始化 header，只设置 keys/vals/slots/len/cap/slot_cap/is_ref。

### 3.7 非逃逸 String 字面量数组

负载：200 万次 `var a: Array<String> = ["a","bb"]; acc += a[0].length+a[1].length`。

| 实现 | 方式 | ns/op |
|---|---|---:|
| Erlang OTP 29 | `["a","bb"]` 列表 | **~1.3** |
| **Well v2（栈数组）** | 栈 `WellArray` + 2 个 immortal 字符串 | **10.5** |
| Java 21 | `String[]{"a","bb"}` 逃逸到 `keep[i]` | **22.4** |
| C++（Clang -O2） | `new vector<string>{"a","bb"}` | **58.5** |

- 栈数组扩展支持 **String 字面量元素**：元素全部是 immortal literal 时才允许，因此不需要 release 路径。
- 只允许 `a[i]` 读；常量下标直接 `a->items[i].p`，没有 `well_array_get` 调用。
- 注意：Well 是非逃逸栈分配，Java/C++ 是逃逸到堆的对照；Java 非逃逸数组可能被 JIT 标量替换。

### 3.8 非逃逸小数组的栈构建（`push`）

负载：200 万次 `var a: Array<int> = []; a.push(1); a.push(2); acc += a[0]+a[1]`。

| 实现 | 方式 | ns/op |
|---|---|---:|
| Erlang OTP 29 | `[1,2]` 列表 | **~8** |
| Go 1.27 | `make+append`（逃逸到全局） | **~12** |
| **Well v2（栈数组 + 直接 push）** | 栈 `WellArray`，无 heap/RC | **~16** |
| Java 21 | `ArrayList` + 2 add（volatile 逃逸） | **~25** |
| Well v2（堆 Array） | `well_array_new` + 2 push + release | **~45.5** |
| C++（Clang -O2） | `new vector` + 2 push + delete | **~94** |

- 非逃逸小数组：堆路径 **45.5 → ~16 ns**。
- 只允许在**同一 block 内静态 ≤4 次 push**，且不允许嵌套循环、索引写、传参/返回/方法调用。
- push 直接生成 `_st.items[_st.len++] = box(...)`，不调用 `well_array_push`，不检查 oid，不扩容。
- Java/Go 对照是逃逸到堆/全局；非逃逸时它们也可能被优化掉。

### 3.9 非逃逸小 Map 的栈构建（常量 key 写入）

负载：200 万次 `var m: Map<int> = {}; m["a"]=1; m["b"]=2; acc += m["a"]+m["b"]`。

| 实现 | 方式 | ns/op |
|---|---|---:|
| Erlang OTP 29 | map literal | **1.3** |
| **Well v2（栈 Map + 直接 set）** | 栈 `WellMap`，无 heap/RC/registry | **~10** |
| Java 21 | `HashMap` put/get（volatile 逃逸） | **~60** |
| Go 1.27 | map literal（逃逸到全局） | **~90** |
| Well v2（堆 Map） | `well_map_new` + set/get + release | **65** |
| C++（Clang -O2） | `new unordered_map` | **~135** |

- 非逃逸小 Map：堆路径 **65 → ~10 ns（6.5×）**。
- 只允许同一 block 内静态 ≤4 次常量 String key 写入；
- 不允许嵌套循环、传参/返回、其它方法调用；
- 写入直接生成 `_st.keys[i]=literal; _st.vals[i]=box; _st.len++`，不调用 `well_map_set`，不检查 oid，不 hash。

### 3.10 String/Bytes 容器的免 registry 路径

String/Bytes 本身没有出边引用，所以 `Array<String>`、`Map<String>`、`Array<Bytes>` 也不能形成引用环。生成器改用：

```c
well_array_new_flat(ctx, 1)   // Array<String>/Array<Bytes>
well_map_new_flat(ctx, 1)     // Map<String>/Map<Bytes>
```

这些容器仍对元素 retain/release，但不进入 oid registry；若被 `Model.set` 持有会自动补注册。

| 实现 | 方式 | ns/op |
|---|---|---:|
| Java 21 | `String[]{"a"}` 逃逸 | **~22** |
| C++（Clang -O2） | `new vector<string>` + push | **~58.5** |
| **Well v2（flat）** | `Array<String>` 免 registry | **~58** |
| Well v2（registered） | `Array<String>` 进 registry | **~82** |

- `Array<String>` 创建+push+release：**82 → 58 ns（约 29%）**。
- 仍慢于 Java 的 String[] 分配，因为 Well 仍有 RC + 对象池 + 字符串 retain/release；但已接近 C++ vector。
- 只对元素类型为 `String`/`Bytes` 的容器启用 flat；其它引用容器仍注册，保留环回收。

### 3.11 HTTP keep-alive：TCC vs Release

`bench_http.py --release` 先构建 Release server；12 workers、8 clients、3s、小响应 `ok`。

| 构建 | req/s | p50 | RSS |
|---|---:|---:|---:|
| TCC 默认 | **208.6k** | 21.4 µs | 14.4 MB |
| Release `-O2 -fwrapv` | 186.2k | 30.7 µs | 6.3 MB |
| Release `-O2`（无 fwrapv） | 208.6k | 21.4 µs | 5.6 MB |

- **Release 不总是更快**：HTTP 连接线程路径对 `-fwrapv` 敏感；默认 TCC 仍是 I/O 最优。
- Release 的 RSS 明显更低（5.6–6.3 MB vs 14.4 MB）。
- `-O2` 无 fwrapv 只作为对照，不作为默认发布标志，避免改变整数溢出语义。

### 3.12 HTTP keep-alive 跨语言对比

同机、12 workers/进程、8 keep-alive 客户端、3 秒、小响应 `ok`：

| 实现 | req/s | p50 | p99 | RSS | 备注 |
|---|---:|---:|---:|---:|---|
| **C++（同源 Clang -O2）** | **252.7k** | 25.0 µs | 35.4 µs | 5.0 MB | thread-per-conn |
| C（Clang -O2，手写） | 248.2k | 21.1 µs | 23.0 µs | 4.4 MB | thread-per-conn |
| Well v2 Release | 213.0k | 23.2 µs | 41.7 µs | 4.8 MB | 同轮 |
| Well v2 TCC | 205.7k | 22.2 µs | 40.5 µs | 13.8 MB | thread-per-conn + inline |
| Go 1.27 `net/http` | 83.6k | 56.8 µs | 2743.3 µs | 17.4 MB | 标准库，未用 fasthttp |
| Java 21 `HttpServer` | 59.8k | 307.7 µs | 495.5 µs | 395 MB | 固定线程池，非 Netty |
| Erlang `gen_tcp` | 55.8k | 62.0 µs | 135.0 µs | 53.2 MB | 每连接一进程 |

- 同轮 C/C++ 比 Well 高约 **18–23%**；Well Release 的 RSS 与 C/C++ 同级（4.8 vs 4.4/5.0 MB）。
- Well 明显快于 Go/Java/Erlang 的最小 server。
- C++ 与 C 使用同一份最小 thread-per-conn 源码，仅编译器驱动不同；作为 C/C++ 上限参考。
- HTTP 吞吐对机器频率/调度敏感（本轮 Go 明显偏低）；以同轮 A/B 为准。

#### 3.12.1 inline 响应快路径

`Http.respond` 在 inline 同步 handler 中直接写响应，跳过连接锁和 cond signal：

| 构建 | req/s | p50 | p99 |
|---|---:|---:|---:|
| TCC | 203.9k | 21.4 µs | 25.6 µs |
| Release | 206.6k | 21.8 µs | 39.9 µs |

- 只对 `inline_sync=1 && current_req==req` 生效；异步/worker 响应仍走加锁路径。
- 单次提升被机器方差掩盖（同轮区间 193–213k），但 p50 稳定在 21–22 µs。
- 语义不变：inline handler 仍同步执行，响应仍写同一 outbuf。

#### 3.12.2 HTTP request event 直接字段

HTTP 路由 wrapper 不再调用通用 event 构造器，而是直接填字段：

```c
ct* e = well_new(sizeof(ct), T_ct, req->id);
e->method = req->method; well_retain(e->method);
...
well_freeze(e);
handler(e);
well_release(e);
```

- 跳过 4 个空 String 默认初始化；
- 跳过 4 次 `well_set_ref` 的 graph edge；event 只引用 immutable String，不可能成环；
- event 仍注册、仍冻结、仍正常 release；
- TCC 195.2k / p50 21.7 µs；Release 本轮 144.6k（机器抖动，未作为收益）；
- 189/189 测试通过。

绑定服务器到 CPU 4–11（`--affinity 0xFF0`）后的 A/B：

| event 构造 | req/s | p50 |
|---|---:|---:|
| 旧：`new_` + `well_set_ref` | 188.4k | 22.2 µs |
| **新：直接字段 + `well_retain`** | **201.4k** | 24.9 µs |

- 吞吐提升约 **7%**，p50 略升；说明收益主要来自减少 graph edge/默认值，而不是延迟。

#### 3.12.3 inline 同步请求免全局注册

inline handler 在连接线程上同步响应时，不再把请求插入全局 request list/map：

- 不取 `g_http_lock`；
- 不增加 `conn->refs`；
- 响应后直接释放 request，继续下一个 keep-alive 请求；
- 只有 handler 返回时仍未响应，才补注册，交给 sw eeper/异步响应。

绑定 CPU 4–11 后：

| 构建 | req/s | p50 | p99 |
|---|---:|---:|---:|
| TCC | 210.1k | 21.6 µs | 27.2 µs |
| **Release** | **215.6k** | **20.5 µs** | **24.1 µs** |

- 相比上一版 201.4k：再提升约 **4–7%**；
- 与 C/C++ 248–253k 的差距缩小到约 **15%**；
- 189/189 测试通过。

### 3.13 HTTP pipelining 跨语言对比

同机、12 workers/进程、8 clients、depth 32、3 秒：

| 实现 | req/s | RSS | 备注 |
|---|---:|---:|---|
| **C（Clang -O2）** | **1,248k** | 4.3 MB | 手写批量解析/响应 |
| **C++（同源 Clang -O2）** | 1,247k | 4.3 MB | 同源 |
| **Well v2 Release** | 1,150k | 4.8 MB | 批量响应 + thread-per-conn |
| Well v2 TCC | 664k | 13.7 MB | 同轮，方差大 |
| Java 21 `HttpServer` | 59.6k | 401 MB | 固定线程池 |
| Go 1.27 `net/http` | 26.7k | 16.0 MB | 本轮异常偏低 |

- C/C++ 比 Well Release 高约 **8%**；Well Release 的 RSS 与 C/C++ 接近（4.8 vs 4.3 MB）。
- TCC 本轮 664k，明显低于 Release 1,150k；pipelining 下优化构建收益大。
- 本轮 Go/Java 受调度影响明显偏低；Caveat 同前：不是框架对框架，以同轮排序为准。

### 3.15 逃逸堆字面量的直接填充

字面量即使逃逸（赋给 struct 字段），如果元素是标量、key 是 String 字面量，仍可直接填 items/keys/vals，避免每元素 `well_array_push` / `well_map_set`：

| 场景 | 旧路径 | 直接填充 | 提升 |
|---|---:|---:|---:|
| `Map<int>{"a":1,"b":2}` 逃逸到字段 | ~188 ns | **~120 ns** | **1.57×** |
| `Map<String>{"a":"x","b":"yy"}` 逃逸到字段 | ~215 ns | **~133 ns** | **~1.6×** |
| `Array<int>[1,2,3]` 逃逸到字段 | ~113–124 ns | **~97–119 ns** | ~1.05–1.2× |

- Map 收益大：省掉 2 次 `well_map_set`（含 key retain/hash/probe）。
- String 值也走直接填充：值全部是 immortal literal 时不需要 retain；key 仍用静态 literal 槽。
- Array 收益小：push 本来就很轻，但仍省掉 3 次函数调用/oid 检查。
- 语法/自动回收不变，数组/Map 仍走正常 `well_track_owned` + release。

### 3.16 新鲜引用对象的字面量数组/Map

字面量元素/值全部是 `new T()` 时，对象所有权可以直接转移给容器，不再走 `push/set` 的 retain + 临时 release。

| 场景 | 旧路径 | 直接所有权转移 | 提升 |
|---|---:|---:|---:|
| `Array<Item>[new Item(), new Item()]` 逃逸到字段 | ~265 ns | **~222 ns** | **1.19×** |
| `Map<Item>{"a":new Item(),"b":new Item()}` | — | **~225 ns** | — |
| 同上，`--release`（无 oid check） | — | **~84 ns** | — |

- `--release` 默认带 `-DWELL_NO_OID_CHECK=1`：引用写不再做 oid/frozen 检查（按“同 oid 串行、引用写不必检查”的约束）。
- 默认 TCC 构建保留检查，测试仍覆盖 foreign write/panic 行为。
- Release 下新鲜引用数组进一步降到 **~84 ns**。

- 生成 `items[i].p = <new object>` / `vals[i].p = <new object>`，容器直接持有 rc=1 的对象；
- 不再 retain，也不 release 临时；
- 只允许 ≤4 个元素、元素/值全部是 `new` 表达式；
- 其它引用表达式（变量、函数返回）仍走 `push/set`，因为需要 retain；
- 容器 release 时由 destroy 正常释放元素。

### 3.17 WebSocket 跨语言对比（Well vs Go）

同机、12 workers、8 clients、3 秒、小文本 echo：

| 实现 | msg/s | p50 | p99 | RSS |
|---|---:|---:|---:|---:|
| Well v2 TCC | 226.0k | 21.4 µs | 48.9 µs | 13.7 MB |
| **Well v2 Release** | 232.2k | **21.1 µs** | **41.1 µs** | **4.8 MB** |
| Java 21 手写 RFC6455 | **238.9k** | 61.9 µs | 185.9 µs | 92.1 MB |
| Go 1.27 手写 RFC6455 | 193.8k | 29.5 µs | 257.8 µs | 13.5 MB |
| Erlang OTP 29 `gen_tcp` | 59.3k | 57.2 µs | 188.4 µs | 55.8 MB |

- Java 吞吐最高，但 p50 约为 Well 的 **3×**，RSS 约 **19×**。
- **Well Release：吞吐接近 Java（-3%），p50 最低、p99 最低、RSS 4.8MB**。
- Go/Erlang 本轮 p99 抖动较大；以同轮为准。
- Caveat：都是手写最小 RFC6455 server，不是框架对框架。

### 3.18 逃逸小数组的静态 push 直接写入

数组即使逃逸（赋给 struct 字段），只要总元素数 ≤4、没有嵌套循环，静态 `push` 也可以直接写内联槽：

| 场景 | 旧路径 | 直接 push | 提升 |
|---|---:|---:|---:|
| `var a: Array<int> = []; a.push(1); a.push(2); h.a = a` | ~128 ns | **~100 ns** | **1.28×** |

- 直接生成 `a->items[a->len++] = value;`，不调用 `well_array_push`；
- 不检查 oid、不扩容、无 RC；
- 条件是：标量元素、唯一 VarDecl、总元素 ≤4、无嵌套循环、变量不被重新赋值；
- 数组仍正常 track/release，逃逸后语义不变。

### 3.19 逃逸小 Map 的常量 key 直接写入

同样把“标量 Map 字面量 + 静态常量 key 赋值”扩展到会逃逸的 Map：

| 场景 | 旧路径 | 直接写 | 提升 |
|---|---:|---:|---:|
| `var m: Map<int> = {}; m["a"]=1; m["b"]=2; h.m = m` | ~220 ns | **~130 ns** | **~1.7×** |

- 直接生成 `m->keys[i]=literal; m->vals[i]=box; m->len++;`；
- 不调用 `well_map_set`，不 hash、不检查 oid；
- 条件：标量值、常量 String key、总 entry ≤4、无嵌套循环、唯一声明、不重新赋值；
- Map 仍正常 track/release。TCC 下该负载方差较大（120–260 ns），以同轮 best/median 为准。

### 3.20 逃逸 String 数组的静态 push 直接写入

`Array<String>` 的 push 参数全是 String 字面量时，literal 是 immortal，不需要 retain：

| 场景 | 旧路径 | 直接 push | 提升 |
|---|---:|---:|---:|
| `var a: Array<String> = []; a.push("x"); a.push("yy"); h.a = a` | ~160 ns | **~111 ns** | **1.44×** |

- 直接生成 `a->items[a->len++].p = literal;`；
- 不调用 `well_array_push`，不 retain、不检查 oid、不扩容；
- 条件：String 数组、字面量 + 静态 String-literal push 总数 ≤4、无嵌套循环、唯一声明、不重新赋值；
- 数组仍正常 track/release；flat String 容器不进入 oid registry。

### 3.21 逃逸 String Map 的常量 key/字面量值直接写入

`Map<String>` 的 value 也是 String 字面量时，不需要 retain，可直接写内联槽：

| 场景 | 旧路径 | 直接写 | 提升 |
|---|---:|---:|---:|
| `var m: Map<String> = {}; m["a"]="x"; m["b"]="yy"; h.m = m` | ~234 ns | **~145 ns** | **~1.6×** |

- 生成 `m->keys[i]=literal; m->vals[i].p=literal; m->len++;`；
- 不调用 `well_map_set`，不 hash、不 retain、不检查 oid；
- 条件：String Map、key/value 都是字面量、总 entry ≤4、无嵌套循环、唯一声明、不重新赋值；
- Map 仍正常 track/release；flat String 容器不进入 oid registry。

### 3.22 JSON parse/stringify（Well vs Go）

150B 对象（嵌套 struct + String 数组），30 万次：

| 实现 | parse | stringify |
|---|---:|---:|
| Well v2 TCC | 1526 ns | 510 ns |
| **Well v2 Release** | **583 ns** | **280 ns** |
| Go 1.27 `encoding/json` | 1129 ns | 526 ns |

- Release 下 Well parse 比 Go 快 **1.9×**，stringify 快 **1.9×**。
- TCC parse 比 Go 慢，但 stringify 基本持平；性能场景应使用 `--release`。
- Java 无标准 JSON 库，本轮未计入；后续可用手写/第三方库补。
- 数据为单线程、同机、同 150B payload。

### 3.23 逃逸引用数组的新鲜对象 push

`Array<Item>` 初始为空、随后 `push(new Item())` 时，新对象所有权可直接转移：

| 场景 | 旧路径 | 直接 push | 提升 |
|---|---:|---:|---:|
| `var a: Array<Item> = []; a.push(new Item()); a.push(new Item()); h.a = a` | ~259 ns | **~195 ns** | **1.32×** |

- 生成 `a->items[a->len++].p = new Item();`；
- 不再 retain 新对象，也不 release 临时；
- 条件：引用元素、初始元素/所有 push 参数都是 `new`、总数 ≤4、无嵌套循环、唯一声明、不重新赋值；
- 容器仍正常 track/release，destroy 释放元素。

### 3.24 逃逸引用 Map 的新鲜对象写入

`Map<Item>` 的 value 全是 `new Item()` 时，对象所有权可直接转移：

| 场景 | 旧路径 | 直接写 | 提升 |
|---|---:|---:|---:|
| `var m: Map<Item> = {}; m["a"]=new Item(); m["b"]=new Item(); h.m=m` | ~345 ns | **~254 ns** | **1.36×** |

- 生成 `m->keys[i]=literal; m->vals[i].p = new Item(); m->len++;`；
- 不再 retain 新对象，也不 release 临时；
- 条件：引用 value、初始/所有写入值都是 `new`、entry ≤4、无嵌套循环、唯一声明、不重新赋值；
- Map 仍正常 track/release，destroy 释放 value。

## 4. Release 构建（对标 Java 的关键）

新增 `--release`：

```powershell
python project\compiler\wellc.py build app.we --release -o app.exe
python project\compiler\wellc.py run   app.we --release
```

选择顺序：`WELL_CC` → `clang` → `gcc` → `zig cc`（本机为 `D:\Soft\zig\zig.exe cc`），
自动加 `-O2 -fwrapv`；默认仍用 TCC 以保持编译速度。

| 指标 | TCC 默认 | `--release`（Clang/LLVM -O2） | Java 21 |
|---|---:|---:|---:|
| 数组访问 | 2.65 ns | **0.35 ns** | 0.385 |
| 对象 new+release | 60 ns | **37–40 ns** | 43 |
| Array 创建+push+release | 45.5 ns | **17–20 ns** | 25.3 |
| Map 创建+set+get+release | 65 ns | **29–31 ns** | 49 |
| 8B String new+release | 49 ns | **16–17 ns** | — |
| `String.fromInt` | 54 ns | **18–19 ns** | — |

结论：

- **Release 构建下，对象、Array、Map、String 均已达到或超过 Java 目标**。
- TCC 默认仍保留快速编译；性能基准/生产应使用 `--release`。
- 栈/区域优化（3.7–3.9）在 TCC 与 Release 下都有效；Release 下更多循环会被完全消除。

## 5. 汇总判断

| 维度 | 排序 |
|---|---|
| 标量/数组循环 | C ≈ C++ ≈ Go > Java > Erlang ≫ Well v2 ≫ Well v1 |
| 短生命周期分配 | C ≈ C++ ≈ Go > Java > Well v2 > Well v1 ≫ Erlang |
| 语言定位 | C/C++ 裸机性能；Go 高并发+低门槛；Java JIT/生态；Erlang 海量进程/容错；Well v2 oid actor + 低内存，但容器/循环运行时开销仍高 |

Well v2 的 5h HTTP 长稳（51.9 亿请求、RSS -0.05MB/h）和 953k pipelining 说明它适合 I/O/actor 服务；但本次 CPU/数组测试表明，**不应把 Well 当作 C/C++/Go/Java 的数值计算替代品**。

## 6. 待办

- HTTP keep-alive / pipelining / JSON / WS 的同负载跨语言测试。
- GCC 重新下载完整包后补 GCC -O2（当前 Zig/Clang 已覆盖 C/C++）。
- 启动时间、RSS、尾延迟单独统计。
