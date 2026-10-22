# Well 语言与运行时 —— 给 AI 的完整参考

> 版本：2026-10-22（基线：112 个 `.we` 用例 + 12 个 C 测试全绿；最新基线以 `python tools/verify.py` 输出为准）
>
> 读者：需要阅读、修改、生成或评审 Well 代码的 AI（以及人类工程师）。
>
> **本文以当前实现为准**。若与旧文档冲突（例如 `IMPLEMENTATION.md` §6 的“已知边界”里有若干已过时条目），以本文 + 源码为准；修改后请更新本文与 `修改日志.md`。

---

## 0. 给 AI 的 14 条铁律（先看这个）

1. **oid 是唯一的数据所有者**：一个对象/事件/容器只属于一个 oid；handler/controller 只能写 `ctx.oid` 拥有的数据（static/装配期数据另有规则）。
2. **禁止跨 oid 引用业务对象**：字段赋值、容器插入都会检查 `value.oid == owner.oid`（String 例外）；违反会 panic `well oid violation`。
3. **`Model.get` 返回 owned 引用**（可空）；交给变量/帧日志管理，不要手工 `free`，也不要长期缓存裸指针。
4. **TTL 回收以 `exec_ref` 租约为准（不是 rc）**：清理只在 `exec_ref==0` 且调度器无该 oid 任务时发生；handler/`Model.get/peek` 会 pin，在飞 event 会 pin 其引用的 Model oid，普通字段/容器禁止跨 oid，static 只允许装配期 oid 对象——因此**正常 Well 代码不会因 TTL 悬空**。C/FFI 裸指针未 `well_oid_pin` 属例外。
5. **handler 是 run-to-completion**：没有 `await/yield`；在 handler 里做阻塞 I/O（File/MySQL/大循环）会卡住该 oid 的 runner，也会占住一个 worker。
6. **controller 的 `ctx.oid == 0`（装配期 oid）**：允许在 `run()` 里 `new` + `Model.set` 创建**系统级只读数据**（oid=0）；handler 可用 `Model.get(T, 0)` 读取，但字段/容器/Bytes 写会 panic（oid 0 ≠ handler oid），TTL/强制清理都跳过 oid 0；**可写**业务数据应在事件 handler 中以事件 oid 创建。
7. **event 构造完成后被冻结**：字段只读；不要保存 event 到 static/跨 oid 结构；`emit` 会改写 event 的 oid。
8. **static 字段只在 `well_run_loop` 之前可写**（装配期，ctx.oid == 装配期 oid）；运行期写/原地改容器/Bytes 会 panic；static 引用类型不能是 event。
9. **没有异常/Result/try-catch**：handler panic 被隔离（继续调度）；controller/装配期 panic **exit 70**；可恢复的 JSON 解析用 `Json.tryParse`。
10. **整数 `/`、`%` 除零会 panic**（带源码行号）；运行期整数溢出是 C wrap；字面量常量溢出/除零是编译错误。
11. **String 的 `length`/下标/切片都按字节**；UTF-8 码点用 `codePointAt`/`fromCodePoint`；大量拼接用 `Bytes`，不要 `String +` 循环（O(n²)）。
12. **容器字面量 `[]`/`{}` 支持：`var` 初始化、下标赋值、static 字段默认值/启动期 static 赋值**；不能直接当函数实参/普通 struct 初始化字段（会编译错误），先赋给变量。
13. **继承现状**：`struct`/`event` 可用 `extends` 复用基类**字段**（仅字段展平，无多态/子类型）；`controller`/`handler` 继承**未实现**（父类逻辑不会执行，不要写）。
14. **写完必须验证**：`python wellc.py build x.we -o x.exe`，项目级回归用 `python tools/verify.py`；测试用例格式见 §11。

---

## 1. Well 是什么

Well 是一个**单进程、面向 oid 的 actor 风格语言**：源码 `.we` 先由 Python 前端编译为 C99，再由 TinyCC（默认）或 gcc/clang 编译为可执行文件。它把以下东西做进了语言/运行时：

```text
oid actor 调度（每 oid FIFO、同 oid 单 runner、跨 oid 并行）
+ 事件总线（on/once/off/emit）
+ Model/Cache/Store（内存行存储 + 64 分片 Cache）
+ 原子引用计数（RC）+ oid TTL 强制整组回收（允许环）
+ 固定大小对象池
+ HTTP/1.1 / WebSocket / Timer / MySQL / JSON / File 内置运行时
+ panic 隔离 + 源码位置
```

**设计目标**：小、快、可预测、部署简单；用“oid 内串行”换取无数据竞争的业务代码；用 RC + TTL 代替 tracing GC；用 C 运行时换取无 VM 依赖。

**不是目标（当前）**：通用生态、JIT、抢占式调度、跨节点集群、完整异常模型、包管理、热更新、生产级 HTTP/WS 特性（TLS/chunked/背压）。

---

## 2. 快速使用

```bash
cd project/compiler

python wellc.py run   examples/hello.we
python wellc.py build examples/features.we -o build/features.exe
python wellc.py emit-c examples/hello.we -o build/hello.c   # 只生成 C

# 全量回归（推荐）：112 个 .we 用例 + 12 个 C 测试
python tools/verify.py            # 全部
python tools/verify.py --quick    # 只 .we 用例
python tools/verify.py --full     # 全部 + HTTP bench smoke

# 性能基线（Windows 本机参考，见 §6）
python tools/bench.py --scenario all --json build/bench/result.json
```

工具链查找顺序：`WELL_CC` 环境变量 → 仓库内 `tool_ai/tools/tcc/...`（Windows 存在时，默认 TCC）→ PATH 中 `tcc`/`gcc`/`clang`。要使用 gcc：`WELL_CC=gcc`（自动 `-O2`）。额外参数用 `WELL_CFLAGS`；gcc/clang 默认自动 `-O2`，TCC 无优化（显式 `WELL_CFLAGS=""` 可关闭自动 `-O2`）。

编译流水线：

```text
.we → lexer → parser → checker → codegen(Python) → build/<name>.c
    + runtime/core/*.c + 用到的 runtime/modules/{http,ws,mysql}/*.c
    → TinyCC/gcc → 可执行文件
```

生成的 C 保留在 `build/<name>.c`，是排查 codegen 问题的一手材料。

---

## 3. 语法参考

### 3.1 文件、module、import、entry

```we
module demo.hello;          // 解析后忽略，不参与命名
import "lib/util.we";       // 相对路径
import lib.util;            // 点路径（解析为 lib/util.we 或 lib/util/index.we）

entry MainController;       // 生成 main()：初始化运行时、注册类型、启动入口 controller
```

- `import` 的声明会合并（同一文件内的重复声明会报错）；
- `entry` 决定 `main()` 启动哪个 controller；没有 entry 无法生成可执行入口；
- 一个文件可以有多 controller；其他 controller 通过 `start(C)` 启动。

### 3.2 类型与值

| 类型 | C 表示 | 说明 |
|---|---|---|
| `bool` | `int` | `true/false` |
| `int` | `int` (32-bit) | 字面量超过 int32 自动按 `long` |
| `long` | `long long` (64-bit) | |
| `float`/`double` | `double` | 两个名字都映射 double |
| `String` | `WellString*` | 不可变、驻留字面量、字节语义、引用类型、可 null |
| `Bytes` | `WellBytes*` | 可变字节缓冲、引用类型 |
| `Array<T>` | `WellArray*` | 同质容器，引用类型 |
| `Map<T>` | `WellMap*` | String 键、值同质，保留插入序 |
| `Linked<T>` | `WellLinked*` | 双向链表，首尾 O(1)、get O(n) |
| `struct S` | `S*` | 字段对象；可 `extends` 用户 struct，基类字段展平；引用类型 |
| `event E` | `E*` | 事件对象；可 `extends` 用户 event，基类字段展平；类型本身即事件身份 |
| `void` | `void` | 仅返回类型/表达式 |
| `null` | `void*` | 所有引用类型可为 null |

- **没有 enum 类型**：enum 只是 int/String 常量集合；不能声明 `var x: Status`。
- **没有 char/short**；`float` 只是 double 别名。
- 所有引用类型默认值：字段/局部变量为 `null`；String 字段默认 `""`（构造时生成）。
- String 的 `length`、`s[i]`、`substring/slice`、`indexOf` 等全部按**字节**；`s.codePointAt(i)`、`String.fromCodePoint(cp)` 才按 UTF-8 码点。

### 3.3 声明

#### struct / event

```we
struct Base {
    id: long;
}

struct User extends Base {
    name: String;             // 默认 ""
    tags: Array<String>;      // 默认 null
}

event BasePing {
    n: int;
}

event Ping extends BasePing {
    msg: String;
}
```

- `struct`/`event` 支持**单继承字段展平**：checker 递归把基类字段拼进子类布局（`Derived` 拥有 `Base` 的全部字段）；根类型 `extends Vo`/`extends Event` 可省略（无实际作用）；
- 继承只复用字段：没有方法继承、没有多态/子类型（`Derived` 不能赋给 `Base`）、没有虚调用；struct/event 都没有用户方法；
- 未知基类、循环继承、重复字段都是编译错误；
- event 的身份仍是子类自己的 `type_id`：`on(BaseEv)` 不会收到 `DerivedEv`（按精确 type_id 派发），不要依赖事件多态；
- `controller`/`handler` 的 `extends` 未实现：语法会被接受但父类 `run`/`onEvent` 不会执行（不要使用）。
- 字段默认值支持字面量/const；static 字段额外支持容器字面量/`new Bytes()/Array/Map`（见 static）。
- 构造：
  - `new User()`：`oid = ctx.oid`；
  - `new Ping(targetOid)`：事件 oid = targetOid；**targetOid 传 0 表示“emit 时生成新 oid”**；省略参数则用 ctx.oid；
  - `new Ping(oid) { n: 1 }` 对象初始化只允许在 `new` 后；event 构造完成后冻结。

#### const / enum

```we
const MAX: int = 10;

enum Status { IDLE; RUNNING; DONE; }        // 自动 0,1,2
enum Color { RED = "red"; BLUE = "blue"; }  // String 枚举

print(Status.fromInt(1));       // 1（未知 -> 0）
print(Status.toString(1));      // RUNNING（未知 -> ""）
print(Color.toString("red"));   // RED（String 枚举只有 toString）
```

- enum 成员用 `;` 分隔；int 枚举可显式赋值、自增；重复 int 值编译错误；
- 纯 int/String 常量，不是独立类型。

#### controller

```we
controller Main {
    run() {
        on(Ping, PingHandler);
        Http.route("/hello", "GET", HelloHandler);
        Http.listen(8080);
    }
}
```

- 单例；`start(Main)` 幂等（只执行一次 `run`）；`stop(Main)` 注销该 controller 的订阅并允许重新 start；
- `run()` 在 `well_run_loop` 之前执行，`ctx.oid = 0`（装配期 oid）；适合注册路由/订阅、初始化 static；
- **允许**在 `run()` 里 `new` 对象并 `Model.set` 创建**系统级只读数据**（oid=0）：它可被任意 handler 通过 `Model.get(T, 0)` 读取，运行期不可写（字段/容器/Bytes 写会 panic），永远不会被 TTL/强制清理回收，进程退出时统一释放；
- 需要运行期修改的业务数据应在事件 handler 中以事件 oid 创建。

#### handler

```we
handler PingHandler {
    onEvent(e: Ping) {
        print("ping", e.n);
    }
}
```

- 方法名也接受 `handle`/`on`/`handler`；
- 参数必须是事件类型（`Event` 或用户 event）；无字段；
- 一个 handler 的 `onEvent` 事件类型用于 `on(H)` 时自动推断。

#### func / namespace / static

```we
func add(a: int, b: int): int { return a + b; }

namespace Math {
    max(a: int, b: int): int { if (a > b) { return a; } return b; }
}

static Config {
    limit: int = 100;
    title: String = "well";
    arr: Array<int> = [1, 2, 3];

    bump(): int {
        limit = limit + 1;     // 仅启动期可写
        return limit;
    }
}
```

- `func` 生成带隐藏 `WellCtx*` 的 C 函数；调用自动传 ctx；
- `namespace` 只放函数，`N.f(...)` 编译期解析为 static C 函数，零运行时开销、不占 oid/RC；
- `static` 是“配置/工具类”：不可实例化；字段启动期可写、运行期只读；自动 release 旧引用；字段类型不能是 event；运行期写会 panic；static 数据永不参与 TTL 回收；
- 启动期（controller `run`，ctx.oid == 0）可以写 static 字段/容器字面量：`Config.arr = [1,2,3];`，运行期不行。

### 3.4 语句

```we
var x: int = 1;               // 可省略类型：var y = 2;
x = x + 1;
if (x > 0) { ... } else { ... }
while (x < 10) { x = x + 1; }
for (var i: int = 0; i < 3; i = i + 1) { ... }
for (v of arr) { ... }        // Array/Linked 值迭代
for (k in map) { ... }        // Map 键迭代（String）

switch (x) {
    case 1: print("one");
    case 2: print("two"); break;
    default: print("other");
}
return x;
break;
continue;
```

switch 规则：
- 支持 `int/long/String`；
- **无 fallthrough**：每个 case 自动 break；
- case 值必须是字面量/const/枚举成员（变量不行），重复 case 报错；
- switch 计入 breakable：`break` 退出 switch；`continue` 只作用于外层循环。

### 3.5 表达式与运算符

```we
1 + 2 * 3          // 算术
a == b, a != b, a < b, a <= b, a > b, a >= b
a && b, a || b, !a
cond ? x : y
"a" + "b"          // String 拼接（O(n²)，大量拼接用 Bytes）
arr[i], map["k"], obj.field
new User(), new Ping(oid) { n: 1 }, new Bytes()
[1, 2, 3], {"k": 1}      // 容器字面量：var 初始化、下标赋值、static 默认值/启动期赋值
Model.get(User, oid), Json.tryParse(User, s), Http.respond(...)
```

- 整数 `/`、`%` 生成 checked helper，除数为 0 → panic（带 `file:line`）；
- 字面量常量除零/常量溢出 → 编译错误；运行期溢出按 C wrap；
- String 临时值参与链式表达式（`.length`、下标、`+`、再调用方法）会自动释放；赋给变量后按 RC 管理；
- **不能**把容器字面量直接当函数实参/普通 struct 初始化字段（编译错误），先 `var` 保存；static 字段默认值与启动期 static 赋值是例外（支持容器字面量）。

### 3.6 事件与 controller 生命周期

```we
controller Main {
    run() {
        on(Ping, PingHandler);          // 订阅 channel 0
        on(Ping, PingHandler, 7);       // 指定 channel
        once(Ping, OnceHandler);        // 至多一次
        off(Ping, PingHandler);         // 注销（按 owner）
        emit(new Ping(ctxOid?) { n: 1 });
        start(Child);                   // 启动另一个 controller（幂等）
        stop(Child);                    // 注销其订阅、允许重启
    }
}
```

- `on(EventType, H[, channel])`；`on(H[, channel])` 省略 EventType（从 handler 参数推断）；
- `channel` 是 `long`：`0` 默认、`>0` 普通、`ALL` 通配（只能用于订阅）；
- `emit(event[, channel])`：event 的 oid 决定进入哪个 oid 队列；不能 emit 到 ALL；
- `once` 在订阅锁内 claim，至多执行一次；`off/stop` 置 inactive，节点不立即释放；
- 派发时冻结 event；事件字段只读；
- 同一 oid 的事件按 FIFO 串行执行；不同 oid 可并行（取决于 `WELL_WORKERS`）。

### 3.7 内建 API 速查

```text
print(...)                         逐个打印 + 换行（不自动加空格）
Model.get(T, oid)                  Cache -> Store -> Cache，返回 owned T*（可 null）
Model.peek(T, oid)                 只查 Cache，返回 owned T*（可 null）
Model.set(obj)                     要求 obj.oid == ctx.oid；write-through
Model.remove(T, oid)               要求 oid == ctx.oid
IdUtil.getId()                     long，雪花式 id
Json.parse(T, String)              T*，失败 panic
Json.tryParse(T, String)           T* 或 null，不 panic
Json.stringify(v[, indent])        String；v 可为 struct/event/null/标量/顶层 Array/Map/Linked
File.read(path) / write(path,text) / exists(path) / remove(path)
Host.nowMs() / Host.env(name) / Host.log(...)
Http.route(path, method, Handler) / Http.listen(port)
Http.respond(reqId, status, body) / Http.stop()
Ws.route(path, OpenH, MsgH, CloseH) / Ws.listen(port)
Ws.send(clientId, text) / Ws.close(clientId[, code, reason]) / Ws.stop()
Timer.once(ms, Handler): long / Timer.loop(ms, Handler): long / Timer.cancel(id)
Mysql.connect/exec/queryJson/close / Mysql.pool/execPool/queryJsonPool/closePool
```

容器/字符串方法：

```text
Array<T>: push/pop/shift/unshift/get/set/slice/concat/join/indexOf/includes/clear/sort/reverse
           下标 a[i]、a.length、for (x of a)
Map<T>:   get/set/has/delete/keys/values/clear、m["k"]、m.size、for (k in m)
Linked<T>:pushHead/pushTail/popHead/popTail/head/tail/get/length、for (x of l)
String:   s[i]/byteAt/charCodeAt/codePointAt、substring/slice、indexOf、contains/
          startsWith/endsWith、trim、toLower/toUpper、split、replace、
          String.fromByte/fromInt/fromCodePoint
Bytes:    new Bytes()、length、push/get/set/byteAt/append/appendStr/clear/toString
enum:     E.fromInt(v)（int 枚举）、E.toString(v)
```

- 越界读：值类型返回默认值（0/false/0.0），引用返回 `null`；越界写 Array 扩展；
- 容器写操作要求 `container.oid == ctx.oid`，插入引用元素要求 `element.oid == container.oid`（String 例外）；
- `Map` 保留插入序；`keys()/values()` 返回新数组；**没有 `entries()`**（语言没有元组/键值对类型）。

### 3.8 错误模型

- **编译错误**：`wellc` 输出 `file:line:col: message`，非 0 退出，不生成 exe；
- **handler panic**：每个 handler 调用有 `setjmp/longjmp` 边界；panic 打印 `well panic: file:line: msg`，随后**只终止当前任务**，调度器继续；该 handler 的帧引用会释放；
- **controller / 装配期 panic**：未被隔离，打印后 **exit(70)**；
- **可恢复解析**：`Json.tryParse` 返回 null；`File`/`Json.parse`/除零/oid 违规等是 panic；
- 没有 try/catch、没有 Result/Option、没有错误码；需要“失败不杀进程”时用可空 API + 前置检查。

---

## 4. 编译与代码生成

### 4.1 前端

```text
wellc/lexer.py    词法（关键字、字符串、数字、注释）
wellc/parser.py   递归下降，生成 ast_nodes
wellc/checker.py  名称/类型/oid 语义检查、内建方法解析、静态分析（return/break）
wellc/codegen.py  生成 C99；类型宏、构造/析构、handler/controller/route/timer wrapper
wellc/driver.py   合并 import、调用 C 编译器、选择运行时模块源文件
```

### 4.2 生成的 C 结构

- 每个 struct/event：`typedef struct T T; struct T { WellObj hdr; fields... }; #define T_T <type_id>`；
- `new_T(WellCtx*, long long oid)`：分配 + 字段默认值（String 默认 `""`）；
- `destroy_T(void*)`：release 引用字段 + `well_obj_free`；
- handler：`static void H_onEvent(WellCtx*, void* evp)`；
- controller：`static void C_ensure(WellCtx*)` / `C_run(WellCtx*)`；
- func/namespace：`static ret f(WellCtx* ctx, ...)`；
- 每个语句前生成 `well_source_set(file, line)`，用于 panic 定位；
- 每个函数入口 `(void)ctx;` 避免未使用告警；
- 模块源文件按程序是否使用 `Http/Ws/Mysql` 选择性加入编译。

### 4.3 编译命令

```bash
python wellc.py build src.we -o out.exe
# 实际执行：cc -I runtime/core -I runtime/modules/http ... -o out.exe build/src.c + runtime 源文件
# Windows 用 TCC；Linux/macOS 可用 WELL_CC=gcc/clang（POSIX 路径尚未正式验证）
```

TCC 0.9.27 是默认验证环境；本机另装了 MinGW-w64 GCC 16.2.0（`D:\Soft\gcc`）。CPU parser 微基准：TCC ≈11–14 µs/parse，GCC -O2 ≈6–8 µs/parse（约 1.8×）；优化参数由 `WELL_CFLAGS`/自动 `-O2` 控制。

---

## 5. 运行时机制

### 5.1 对象头与引用计数

```c
typedef struct WellObj {
    WellAtomicInt rc;       // 原子引用计数
    unsigned int type_id;   // 运行时类型 id
    unsigned int flags;     // IMMORTAL / IN_MODEL / FROZEN
    long long oid;          // 所有者 oid
    void *exec_pins;        // 事件执行 pin
    struct WellObj *reg_next, *reg_prev;  // oid registry
    size_t alloc_size;      // 池 class 大小或 malloc 大小
} WellObj;                  // x64 上约 56 字节
```

规则：

| 事件 | 计数 |
|---|---|
| `new` | 1（调用方持有） |
| 局部变量绑定 | 帧日志：owned 直接记录，borrowed `retain` |
| 字段赋值 | 新值 +1、旧值 -1 |
| `Model.set` | 对象 +1（已存在幂等）；替换旧对象 -1 |
| `Model.get/peek` | +1（返回 owned，帧日志负责 release） |
| `Model.remove` | -1 |
| `emit` | event +1（队列持有） |
| 派发结束 | event -1 |
| 帧结束 | 帧内所有引用 -1 |

- `WellCtx` 持有帧日志：内联 16 个 refs + 4 个 pins，超出才堆分配；每 handler 栈上约 248B 的 ctx；
- RC 原子：retain relaxed、release acq_rel；最后一个 release 负责 `destroy_T`；
- 级联释放：字段引用 release；容器/ строки 等有专门 destroy；
- **允许成环**：运行时不 DFS；环由 oid TTL 整组回收；
- `WELL_RC_DEBUG=1` 时进程退出打印 `well: live objects at shutdown = N`，正常应为 0（测试用此检查泄漏）。

### 5.2 oid 与写入检查（安全模型的核心）

- `oid` 是数据所有者：事件目标、Model 行、容器、对象都由某个 oid 拥有；
- `new Struct()` → `oid = ctx.oid`；controller `run` 的 ctx.oid=0，因此其中 `new` + `Model.set` 得到 oid=0 的系统级只读数据；`new Event(target)` → `oid = target`；
- 字段写：`target.oid == ctx.oid`；引用值写：`value.oid == target.oid`（String 无 oid，跳过；null 允许）；
- 容器写：`container.oid == ctx.oid`；插入引用元素：`element.oid == container.oid`（String 例外）；
- event 构造字段**豁免** oid 检查（事件可以携带异 oid 的只读引用），构造后 `well_freeze` 冻结；
- `Model.set/remove` 要求对象/oid 与 ctx.oid 相等；
- 这些检查是**运行时 panic**，不是编译期保证（静态分析只做有限检查）。

**推论**：业务代码天然按 oid 分区，不需要锁；但**不要跨 oid 共享可变对象**，也不要让 static/模块 C 结构持有运行期 oid 对象。

### 5.3 调度器（oid actor）

```text
qmap: oid -> FIFO queue
每个 worker 有本地 deque；先取本地尾，再偷其他 worker 头，最后取全局 injector
同 oid 同时最多一个 runner（running=1）
异 oid 可并行（worker 数）
```

- 默认 `WELL_WORKERS=1`（确定性测试），可设 2..64；主线程 id=0 也参与；
- 队列空即从 qmap 移除；入队时若队列空闲则 `pending++`，并 push 到 ready；
- `g_pending` 和 `g_service_count` 都为 0 时 `well_run_loop` 退出；
- 事件/任务节点、oid queue 各有 256 上限的空闲池；
- 工作窃取：worker 从本地 deque 尾部取，偷别人头部；相同 oid 串行由 running 标志保证；
- **无抢占**：一个长 handler 会占住一个 worker；该 oid 后续事件等待；
- **cleaning 隔离**：TTL 清理时 queue 标 `cleaning`，期间入队只挂链不调度，清理结束统一 ready + signal；
- 丢唤醒修复：退出判定相关路径用广播 `sched_notify()`，避免主 worker 错过唤醒。

### 5.4 Model / Cache / Store

```text
Model.get(T, oid):   Cache hit -> return owned
                     miss -> Store.select(编码字节) -> decode -> Cache.set -> return
Model.peek(T, oid):  只查 Cache
Model.set(obj):      Store.upsert(编码) -> Cache.set
Model.remove(T,oid): Store.delete -> Cache.remove
```

- Cache：64 分片，key=(type_id, oid)，开放寻址 + 线性探测，负载 0.7，每分片 spinlock；get 在锁内 retain；
- Store 是接口；默认 `MemoryStore` 只存**编码字节**（不是对象指针），线性表 + spinlock；decode 的 ref resolver 只递归本 Store，不查全局 Cache；
- `well_model_use_store` 可替换后端（C 侧），语言层没有多后端 API；
- 全局 Cache 不区分来源；多 Model/Store 可加载同一 key；一致性由用户负责；
- 同一 key 被替换时，旧对象可能仍被 handler/event 持有，不会立即释放；
- **Store 保留数据**：TTL 只驱逐 Cache/内存对象，下次 `Model.get` 从 Store 回源。默认 MemoryStore 也在进程内存里，进程退出即丢；没有自动磁盘持久化。

### 5.5 TTL 与强制整组回收（非常重要）

- 默认 **开启，60 秒**：`WELL_MODEL_TTL_MS` 覆盖，`0`/负数关闭；
- 活动时间由 **`Cache.get/set`（即 `Model.get/peek/findByOid/set`）以及事件派发 `dispatch_event`** 刷新；单纯的 `well_oid_pin` 不刷新 last_active。
- 注意：`Model.peek` 也会 touch（它走 `well_cache_get`），所以**轮询 peek 会把 oid 保活**；不要用 peek 做“只读探测”而误以为不会延长生命周期。
- 清理条件：oid 空闲 ≥ TTL 且 `exec_ref == 0`（没有 handler/Model pin/在飞事件 payload 引用）且调度器无该 oid 排队/运行任务；
- 清理是**整组强杀**：`well_oid_take_objects(oid)` 按 oid 取出 registry 中该组全部对象，`well_force_free_objects` 逐个释放；
- **“不看 rc”的含义**：清理判据是 `exec_ref==0` + 调度器空闲，而不是 rc。语言层的组外引用由 `exec_ref` 与 oid 检查覆盖：
  - handler 帧、`Model.get/peek/findByOid` 通过 ctx pin 该 oid，帧结束才 unpin；
  - queued/running event 在 emit 时 pin payload 中 Model 对象的 oid；
  - 普通字段/容器的跨 oid 写被拒绝；static 值必须是装配期 oid（String 例外），不能持有运行期 oid 业务对象。
  所以正常 Well 代码在 `exec_ref==0` 时不存在组外语言层引用，整组释放不会悬空。
  - **例外**：C/FFI 直接保存 `WellObj*` 且未 `well_oid_pin`、或直接调用内部 `well_force_cleanup_oid` 绕过条件；当前 event payload pin 只对 `WELL_FLAG_IN_MODEL` 对象加 pin（把未 `Model.set` 的局部对象跨 oid 放进 event 属于边角情况，建议业务数据走 Model）。
- 同时丢弃该 oid 的 Cache 行；Store 字节保留；下一次访问从 Store 解码新对象；
- 装配期 oid（当前 0）**永不回收**：TTL 扫描与 `well_force_cleanup_oid` 都跳过；
- 清理是隔离 + 分批的：默认每轮最多 `WELL_MODEL_TTL_BATCH=64` 个 oid；每个 oid：
  1. `g_sched_mutex` 标 queue cleaning；
  2. `g_act_lock` 校验、摘 ActEntry、摘 registry 对象、丢 Cache 行（短暂挡住 pin/Model.get）；
  3. 释放全局锁后 `well_force_free_objects`；
  4. 清 cleaning，恢复入队调度；
- 因此 bulk free 不持 `g_act_lock`/`g_sched_mutex`，其他 oid 不被大组释放阻塞。

**AI 必须记住**：TTL 不是“GC 会等你”；一个 oid 只要 60 秒没有 Cache 活动且没有 handler，就可能整组消失。需要长期数据：保持 oid 活跃（定期 `Model.get/set`）或每次从 Store 回源，不要把裸指针跨过空闲期缓存。

### 5.6 内存管理与对象池

- `well_new(size, type_id, oid)`：
  - size ≤ 4096 走池：按 16B 对齐成 class，**新分配按 class 整块大小申请**（避免同类小分配被大对象复用越界）；
  - 池上限 `WELL_POOL_MAX_MB`（默认 64MB），`WELL_POOL_TTL_MS`（默认 60s）到期由 `well_pool_sweep` 真正 free；sweep 最多约 1 秒一次；
- 回收：`well_obj_free` 按 `alloc_size` 入池；超过 4096B 直接 free；
- String：用 malloc 直接分配，destroy 时 free（不进池）；
- Bytes：分配走 `well_new`（对象池），但 destroy 时 `free(data)` + `free(b)` 直接释放、不回收进池；struct/event/容器走池并正常回收；
- 事件/任务节点、oid queue、HTTP 请求结构有各自的小对象池；
- 局部作用域：`well_ctx_mark/release_from` 在循环体/块结束释放局部引用；覆盖赋值用 `well_ctx_replace`；
- 退出时：static shutdown、事件/Model/模块 shutdown、池清空；`WELL_RC_DEBUG` 可检查泄漏；
- 内存注意：Cache 存对象引用（对象本身）；Store 另存一份编码字节（默认 MemoryStore），所以 Model 数据会有**对象 + 编码字节两份**；TTL 默认 60s，空闲对象在内存里至少留这么久；池最多保留 64MB。

### 5.7 锁、原子与并发安全

| 锁 | 类型 | 保护 |
|---|---|---|
| `g_sched_mutex` | mutex | qmap、ready deques、pending/service、queues |
| `g_act_lock` | spinlock（全局） | 活动表、pin/unpin、TTL 候选 |
| Cache 分片锁 | spinlock ×64 | 每分片哈希表 |
| registry 分片锁 | spinlock | oid registry |
| 模块锁 | mutex | HTTP/WS 连接表/路由等 |
| print 锁 | mutex | print/Host.log 串行化 |
| RC | 原子 | retain/release |

- 锁顺序：清理路径 `act -> sched`（先 act 后 sched）；不要在持有全局锁时调用可能再取锁的用户代码；
- bulk free 不持全局锁（§5.5）；
- 用户 handler 之间没有共享可变数据（按 oid 隔离），所以业务代码通常不需要锁；**同一 oid 内没有并发**；
- 需要跨 oid 共享的只读数据：static 只读、或每次 `Model.get`（注意 TTL）；
- `print` 是全局串行化的，热路径大量 print 会成瓶颈（bench 服务端不要 print）。

### 5.8 panic 与错误边界

- handler 调用用 `setjmp/longjmp` 包裹（线程局部 panic env）；panic 后当前任务结束、帧引用释放、调度器继续；
- 源码位置：codegen 在每个语句前发 `well_source_set(file,line)`，panic 格式 `well panic: <file>:<line>: <msg>`；
- 未捕获（controller/装配期）：`exit(WELL_PANIC_EXIT_CODE = 70)`；
- 模块内部（HTTP/WS reader 线程、Timer 线程）的 panic 不是 handler 边界，通常终止进程（不要把用户逻辑放在这些线程里；用户逻辑都是 handler task）。

### 5.9 static / 装配期

- 启动顺序（生成 main）：运行时初始化 → 类型注册 → `well_static_owner_set(ctx.oid)`（当前 0）→ `well_statics_init()` → 模块注册/初始化 → `well_run_loop`；
- `well_run_loop` 开始时 `well_static_freeze()`：此前 static 可写（且要求 ctx.oid == 装配期 oid），此后只读；
- static 字段值要求 `value.oid == 装配期 oid`（String 例外）；event 不允许放进 static（emit 会改 oid）；
- static 的容器/Bytes 原地变异也受 oid 检查拦截（对象 oid=0 vs handler oid≠0）；
- static 数据永不 TTL 回收，退出时统一 release。

### 5.10 HTTP / WebSocket / Timer 线程模型

**HTTP**

- `Http.listen(port)` 非阻塞：启动 listener 线程 + 超时清扫线程，并 `well_service_hold()`；
- listener 只 accept；每个连接一个 reader 线程，持有一个 service 计数；连接关闭前 run loop 不退出；
- reader 循环读请求（HTTP/1.1 keep-alive；空闲 15s 关闭），每个请求作为 task 投递到“请求 oid”队列，然后等响应写出；
- handler 在 worker 上执行；`Http.respond(reqId,status,body)` 可在任意 handler 调用，**首个响应生效**，后续空操作；
- 5s 内无人响应 → 504 并关闭连接；每连接同时只有一个在飞请求（不支持 pipelining 并发）；
- 限制：header 16KB、body 1MB；无 TLS/chunked；每连接一个线程，高并发连接数受线程数/内存限制；
- 小响应（头+体 ≤4KB）合并一次 send；TCP_NODELAY；
- `Http.stop()` 释放服务；所有连接关闭后 run loop 退出。

**WebSocket**

- `Ws.listen` 非阻塞：listener 线程 + 每连接 reader 线程；每个连接是一个 oid；
- open/message/close handler 作为 task 投递到该连接 oid，同 oid 串行、消息 FIFO；
- `Ws.send/close(clientId, ...)` 可按 clientId 从任意 handler 调用；连接表引用计数 = 表 + 每个 task；
- 握手 SHA1+Base64；text frame、ping/pong、close；客户端必须 mask；
- 不支持分片、二进制、TLS、压缩、同端口 Upgrade、背压、空闲超时；
- `Ws.stop()` 对连接用 `shutdown()` 而非 `closesocket`，让 reader 自己关闭，避免 RST 丢最后帧。

**Timer**

- `core/well_timer.c`：懒启动 timer 线程，约 5ms 轮询；到期以 timer id 作为 oid 投递 task，handler 在普通 worker 上执行；
- `Timer.once/loop` 返回 id；`Timer.cancel(id)`；loop 落后时跳过错过周期，不补发；
- 有 timer 时 timer 线程持 service 计数，进程不会退出；精度约 5–15ms（受 cond/睡眠影响）。

### 5.11 生命周期与退出

```text
main:
  well_init
  注册类型/生成模块
  well_static_owner_set(0); well_statics_init()
  模块 init
  controller ensure（run：注册路由/订阅/启动子 controller）
  well_run_loop（处理事件/task，直到 pending==0 && service==0）
  well_statics_shutdown；well_shutdown（模块逆序关闭、Model/Cache/池释放）
```

- 如果 controller `run` 返回后既没有 pending 事件、也没有 service（无 HTTP/WS/Timer），进程会立即退出；
- HTTP/WS/Timer 的 service 计数是保持进程存活的关键；
- `WELL_RC_DEBUG=1` 在退出时打印 live objects；正常期望 0。

### 5.12 IdUtil

- `IdUtil.getId()`：41 位毫秒时间（epoch 2024-01-01）+ 10 位 serverId + 12 位 sequence；
- `WELL_SERVER_ID` 0..1023；同毫秒 sequence 自增，溢出逻辑时间 +1；
- 时钟回拨用 `max(now,last)`；进程内单调；跨进程/重启仍可能重复（部署需唯一 serverId）。

---

## 6. 性能：CPU、内存、启动（当前实测/推断）

> 数字来自 `tools/bench.py` 的 Windows 本机基线（TinyCC 0.9.27，**Python 客户端**），以及项目内 HTTP parser 对比。它们是**相对回归基线**，不是跨语言公平基准。

### 6.1 本机实测

| 指标 | 数值 | 说明 |
|---|---|---|
| HTTP 单请求延迟 p50 | ≈ 45 µs | keep-alive 顺序请求，Python 客户端，TCC 构建 |
| HTTP 单请求延迟 avg / p99 | ≈ 90 µs / ≈ 895 µs | p99 受 OS 调度/计时抖动影响大 |
| HTTP 吞吐（4 Python 客户端） | ≈ 31k req/s | Python 客户端可能成为瓶颈；更早的 C 客户端测试为 41–60k req/s |
| 编译器前端 lex+parse | ≈ 302 µs/次 | 对 bench server 源文件，Python 侧 |
| Well 实现的 HTTP parser | ≈ 13.3 µs/次 | 对比 C 实现 ≈ 0.75 µs/次，约 18×；主因每字段多次分配（README/性能文档） |
| CPU 微基准 `examples/http_parser_bench` | TCC ≈11–14 µs/parse；GCC 16.2 `-O2` ≈6–8 µs/parse（≈1.8×） | Windows x64，20k 次/轮；GCC 为 MinGW-w64 UCRT posix-seh |

构建产物是 C → 原生 exe，无 VM/JIT 预热；启动即执行。

### 6.2 CPU 成本模型（为什么快/为什么慢）

**快的部分**
- 生成 C + 直接 struct 字段访问；namespace 函数零运行时开销；
- 同 oid 串行 → 业务代码无锁、无原子竞争；
- 对象池复用、事件/队列/请求结构池化，减少 malloc；
- keep-alive、小响应单 send、TCP_NODELAY；HTTP route 缓存；
- 无 tracing GC 的全局暂停；TTL 清理已改为锁外 bulk free。

**慢/贵的部分**
- **TCC 优化很有限**：常见表现是函数调用不内联、循环不做展开/向量化；driver 对 gcc/clang 默认追加 `-O2`（`WELL_CFLAGS` 可覆盖）。本机用 MinGW-w64 GCC 16.2.0 实测 CPU parser 微基准：TCC ≈11–14 µs/parse → GCC -O2 ≈6–8 µs/parse（约 1.8×）；HTTP 网络场景受 Python 客户端/OS 抖动影响，p99 有改善迹象但吞吐无稳定差异；Linux/gcc 尚未正式验证；
- **RC 原子操作**：每次 retain/release 都有原子指令；对象图大/引用密集时成本线性；
- **安全检查**：oid 检查、容器边界/类型、String 字节操作等；
- **每请求分配**：HTTP 请求结构池化，但 event 对象、path/query/body String、容器等仍分配；
- **String 拼接 O(n²)**：循环 `s = s + x` 会复制；用 `Bytes`；
- **同 oid 串行**：单个热点 oid 无法利用多核；需要按 oid 分片/多事件目标；
- **单 worker 默认**：`WELL_WORKERS=1` 时所有事件串行（测试确定性优先）；生产要设大；
- **线程模型**：HTTP/WS 每连接一个 reader 线程；连接多时线程栈与调度开销大（默认栈通常 MB 级），不是 epoll 事件驱动；
- **全局 act spinlock**：高频 `Model.get/pin` 会争用（64 分片 Cache 只保护表，活动表是全局 spinlock）。

### 6.3 内存成本（x64）

| 结构 | 估算 | 备注 |
|---|---|---|
| `WellObj` 头 | ≈ 56 B | rc/type_id/flags/oid/exec_pins/reg 指针/alloc_size |
| `String` | ≈ 80 B + 字节数据 | header + data ptr + hash + len；字面量驻留 |
| `Bytes` | ≈ 80 B + 容量 | 可变缓冲 |
| `Array<T>` | ≈ 88 B + items 数组 | items 是 `WellValue[]` |
| `Map<T>` | ≈ 120 B + keys/vals/slots | 开放寻址 + 插入序数组 |
| `Linked<T>` | ≈ 88 B + 节点 | 每节点额外 malloc |
| `WellCtx` | ≈ 248 B/栈帧 | 内联 16 refs + 4 pins |
| 池保留 | 默认上限 64 MB | 空闲对象块最多留 60s |

- 每个 HTTP 请求会分配请求对象（池化）、event、若干 String；keep-alive 避免连接建立成本；
- `Model` 数据有“对象 + Store 编码字节”两份（默认 MemoryStore）；TTL 60s 内空闲对象不释放；
- 大量长连接/线程会放大内存；没有连接数上限/背压（HTTP/WS 当前限制）。

### 6.4 启动与构建

- 进程启动：运行时 init + 注册类型 + controller run，毫秒级；无 VM 预热；
- 编译：Python 前端 + TCC，通常秒级；TCC 编译快但生成的代码慢；
- 可执行文件依赖编译进去的 C 运行时（HTTP/WS/MySQL 按需），无外部运行时 DLL（Windows 上 Winsock 动态加载）。

---

## 7. 横向对比（CPU / 内存 / 并发 / 生态）

> 下表的星级/描述是**设计取向**，不是同一基准下的测量值；Well 的实测见 §6。

| 维度 | Well | C | C++ | Rust | Go | Java | C# | Node/JS | Python | Erlang/Elixir |
|---|---|---|---|---|---|---|---|---|---|---|
| 执行方式 | Python→C99→TCC/gcc 原生 | 原生编译 | 原生编译 | 原生编译 | 原生编译 | JVM JIT | CLR JIT | V8 JIT | CPython 解释 | BEAM VM |
| 内存管理 | 原子 RC + oid TTL + 池 | 手动 | RAII/手动 | 所有权/借用 | tracing GC | tracing GC | tracing GC | tracing GC | 引用计数 + 分代 GC | 每进程 GC |
| 并发模型 | oid actor，每 oid 串行，worker 池 | pthread 等手动 | 线程/协程库 | 线程/async | goroutine + 抢占调度 | 线程/虚拟线程 | Task/线程 | 单线程 event loop + libuv | GIL 线程 | 抢占式 actor |
| 同一数据竞争 | 语言约束（oid 写检查） | 用户保证 | 用户保证 | 编译期保证 | 用户保证（race detector） | 用户保证 | 用户保证 | 单线程为主 | GIL 保护 | 无共享内存 |
| 错误模型 | panic 隔离 + 可空 API；无异常 | errno/返回码 | 异常 | Result/panic | error 返回 | 异常 | 异常 | 异常/Promise | 异常 | 进程崩溃/监督树 |
| 启动 | 毫秒级 | 毫秒级 | 毫秒级 | 毫秒级 | 毫秒级 | JVM 较慢 | CLR 较慢 | 解释器启动 | 解释器启动 | VM 启动 |
| CPU 效率（原生代码） | 中（TCC 无优化、RC、检查） | 高 | 高 | 高 | 高 | 高（JIT 后） | 高 | 中高（JIT） | 低 | 中高 |
| 内存占用 | 低-中（RC/对象头/池/线程栈） | 低 | 低-中 | 低 | 中（GC 头/运行时） | 高（JVM 堆） | 高（CLR） | 中 | 高 | 中（VM + 进程） |
| 生态/工具 | 极小（无包管理/调试器/IDE） | 成熟 | 成熟 | 成熟 | 成熟 | 成熟 | 成熟 | 成熟 | 成熟 | 成熟 |
| 部署 | 单 exe + C 运行时（按需） | 原生 | 原生 | 原生 | 静态二进制 | JVM | CLR | Node 运行时 | Python 运行时 | BEAM |
| 适合场景 | 小服务、oid 分区业务、原型、嵌入式风格 | 系统/底层 | 系统/游戏 | 系统/安全 | 云服务/并发 | 企业后端 | 企业后端 | IO 密集 Web | 脚本/AI/数据 | 高可用分布式 |

### 与各语言的差异要点

**vs C**
- Well 生成 C，直接使用 C 运行时；性能同源但多了 RC/oid 检查/池/调度，TCC 无优化时明显慢于 `-O2` C；
- C 需要手动管理生命周期；Well 用 RC + 帧日志 + TTL 降低负担，TTL 强杀以 `exec_ref` 租约为准（C 侧裸指针需自行 pin）；
- C 的线程/锁由用户负责；Well 提供 oid actor 和内置 HTTP/WS/Timer，业务代码通常无锁。

**vs C++ / Rust**
- C++/Rust 编译期优化、零成本抽象更强；Well 的 handler/event/Model 是运行时调度，灵活但开销更高；
- Rust 编译期保证内存/线程安全；Well 靠 oid 写检查 + pin 约定，语言层跨 oid/static 有检查，C/FFI 裸指针需遵守 pin 规则；
- Well 的学习/生成成本低（语法小），适合 AI 生成短服务；Rust 生态/安全更强。

**vs Go**
- Go 的 goroutine 轻量、GC 并发、标准库/生态成熟；Well 的“actor”是 oid 队列 + 固定 worker，**handler 不可挂起**，阻塞调用会占 worker；
- Go 的 channel 跨 goroutine 共享内存（需同步）；Well 同 oid 串行、跨 oid 禁止引用，业务模型更受限但更简单；
- Well 启动/内存更小（无 GC 运行时），但 HTTP/WS 线程模型（每连接一线程）不如 Go netpoll 可扩展。

**vs Java / C#**
- JVM/CLR 的 JIT 在热路径上通常显著快于 TCC 生成的代码；GC 无全局长暂停（现代收集器）但堆占用高；
- Well 无 JIT、无 VM；启动快、内存小；RC 是即时释放，但原子操作与 TTL 强杀带来不同语义；
- Java 虚拟线程可挂起阻塞调用；Well handler 不可挂起，长 I/O 会卡 oid/worker。

**vs Node.js**
- Node 单线程事件循环 + libuv 异步 I/O，适合 IO 密集；Well 多 worker + 每连接 reader 线程，CPU 密集可并行（受 oid 串行限制）；
- Node 的 async/await 可挂起；Well 没有 async，阻塞调用会卡；
- Well 的 HTTP/WS 是自写最小实现，无 TLS/chunked/背压；Node 生态成熟。

**vs Python**
- CPython 有 GIL，CPU 密集多线程受限；Well 原生多线程可并行（按 oid）；
- Python 生态/AI 库无可替代；Well 适合生成小型原生服务；
- 两者都有“引用计数”（CPython），但 Well 是原子 RC + TTL 强杀，Python 是 GIL + 分代 GC。

**vs Erlang/Elixir、Akka**
- 都是 actor 风格：消息、单 actor 串行、监督；
- Erlang 是抢占式调度 + 每进程 GC + 不可变消息 + 分布式；Well 是协作式 run-to-completion、共享内存 + oid 检查、单进程；
- Akka 跑在 JVM 上，有成熟生态/集群；Well 更小、无 VM、C 运行时；
- Well 没有监督树/邮箱优先级/背压/分布式。

**vs Rust**
- Rust 编译期所有权/借用，零 GC；Well 用 RC + 运行时检查，写起来更简单但安全边界弱；
- Rust async 可挂起；Well handler 不可挂起；
- Well 的 HTTP/WS/Timer/Model 开箱即用；Rust 需要库生态。

---

## 8. 给 AI 的注意事项与常见错误

### 8.1 写 Well 代码的正确模式

**HTTP 同步响应（最短路径）**

```we
event HttpRequestEvent {
    reqId: long;
    method: String;
    path: String;
    query: String;
    body: String;
}

controller Main {
    run() {
        Http.route("/health", "GET", HealthHandler);
        Http.listen(8080);
    }
}

handler HealthHandler {
    onEvent(e: HttpRequestEvent) {
        Http.respond(e.reqId, 200, "ok");
    }
}

entry Main;
```

**HTTP + 业务 oid 异步响应**

```we
event OrderEvent { reqId: long; }

controller Main {
    run() {
        on(HttpRequestEvent, IngressHandler);
        on(OrderEvent, OrderHandler);
        Http.route("/order", "POST", IngressHandler);
        Http.listen(8080);
    }
}

handler IngressHandler {
    onEvent(e: HttpRequestEvent) {
        emit(new OrderEvent(0) { reqId: e.reqId });
    }
}

handler OrderHandler {
    onEvent(e: OrderEvent) {
        Http.respond(e.reqId, 200, "done");
    }
}
```

**Model 读写（handler 内）**

```we
handler UserHandler {
    onEvent(e: UserEvent) {
        var u: User = Model.get(User, e.uid);
        if (u == null) {
            u = new User();
            u.name = "new";
            Model.set(u);
        } else {
            u.name = "updated";
            Model.set(u);
        }
    }
}
```

- `new User()` 的 oid = 当前事件 oid；`Model.set` 要求 `u.oid == ctx.oid`；
- `Model.get` 返回 owned，交给变量/帧日志；不要跨事件保存裸指针。

**static 配置**

```we
static Cfg {
    limit: int = 10;
    title: String = "svc";
}

controller Main {
    run() {
        Cfg.limit = 20;            // 启动期可写
        Cfg.title = "prod";
    }
}
```

**Timer / WS**

```we
controller Main {
    run() {
        Timer.loop(1000, TickHandler);
        Ws.route("/chat", WsOpen, WsMsg, WsClose);
        Ws.listen(8081);
    }
}
```

### 8.2 绝对不要做的事

| 错误 | 后果 |
|---|---|
| 把需要在运行期修改的数据放进 controller `run` 的 oid=0 `Model.set` | oid=0 系统级只读：TTL 不回收，但 handler 写会 panic；可写数据放在事件 handler |
| 在 C/FFI 中保存 `WellObj*` 但不 `well_oid_pin` | TTL 强杀后悬空；语言层 static/跨 oid 有检查，C 侧需自己 pin |
| 在 handler 里长期保存裸对象指针（超出本次事件/帧） | 对象可能被释放；用 oid + `Model.get` 重新取 |
| 跨 oid 字段/容器引用 | panic `well oid violation`（String 除外） |
| 把 event 放进 static/跨 oid 容器 | event 构造后冻结，且 emit 会改 oid；static 直接禁止 event 类型 |
| 在 handler 里做阻塞 I/O/大循环 | 卡住该 oid 的 runner 和一个 worker；其他 oid 仍可跑，但该 oid 的事件排队 |
| 循环 `s = s + x` 拼大字符串 | O(n²)；用 `Bytes.appendStr` |
| 在热路径 `print` | 全局 print 锁串行化，吞吐骤降 |
| 依赖 `controller`/`handler` 继承 | 继承未实现，父类 run/onEvent 不会跑 |
| 使用 `Map.entries`/用户泛型/接口/元组/闭包/异常 | 未实现，编译错误 |
| 把 `struct`/`event` 继承当多态/子类型用 | 只有字段展平：不能赋给基类、无虚调用；event 订阅按精确 type_id |
| 把容器字面量直接传函数/构造字段 | 编译错误；先 `var` 保存 |
| 在 switch 里期待 fallthrough | 无 fallthrough，每个 case 自动 break |
| 依赖运行期整数溢出检查 | 只有常量检查；运行期 wrap |
| 认为 `String.length` 是字符数 | 是字节数；UTF-8 用 `codePointAt` |
| 认为 `Json.parse` 会返回 null | 失败 panic；用 `Json.tryParse` |
| 认为 TTL 是 GC 会保留对象 | 空闲 60s 后整组消失；需要长期数据要保活或从 Store 回源 |
| 用 `Model.peek` 做“只读探测” | `peek` 也走 `Cache.get`，会刷新 TTL，等于保活该 oid |
| 认为 HTTP/WS 有 TLS/chunked/背压/连接上限 | 都没有；生产需自己在前面加代理/网关 |

### 8.3 可空性与失败路径

- 所有引用类型可 null：`Model.get/peek`、`Json.tryParse`、`Host.env`、`String.split` 的元素等；
- 访问 null 对象字段/方法会 panic（不是编译错误）；
- `File.read/write`、`Json.parse`、整数除零、oid 违规、容器越界写是 panic；
- handler 内 panic 被隔离，测试里表现为 stderr 有 `well panic:` 但进程继续；controller panic 进程 exit 70。

### 8.4 AI 生成代码的检查清单

1. 程序有 `entry C;`，C 是 controller；
2. 事件类型（HTTP/WS/Timer 用的）字段名/类型与内建约定匹配；
3. handler 方法名 `onEvent`（或 handle/on/handler），参数是事件类型；
4. `on/emit/route/listen` 在 controller `run` 中；
5. 可写业务数据在 handler 中创建/读写，oid 正确；系统级只读配置可在 controller `run` 创建并 `Model.set`（oid=0）；
6. 不跨 oid 引用；运行期可写对象不要放 static（系统级只读配置可放 static 或 oid0 Model）；
7. 容器字面量先赋变量；
8. String 大拼接用 Bytes；
9. 除法前检查 0，或接受 panic；
10. 可恢复解析用 tryParse；
11. 最后运行 `python wellc.py build` / `tools/verify.py` 验证。

---

## 9. 环境变量与默认值

| 变量 | 默认 | 作用 |
|---|---|---|
| `WELL_CC` | 自动查找 | 指定 C 编译器（tcc/gcc/clang） |
| `WELL_CFLAGS` | gcc/clang 自动 `-O2`，TCC 空 | 额外 C 编译参数；显式空串可关闭默认 `-O2` |
| `WELL_WORKERS` | `1` | worker 线程数（1..64；主线程也参与） |
| `WELL_MODEL_TTL_MS` | `60000` | 空闲 oid 回收 TTL；`0`/负数关闭 |
| `WELL_MODEL_TTL_BATCH` | `64` | 每轮清理最多 oid 数（1..256） |
| `WELL_POOL_TTL_MS` | `60000` | 空闲对象池块真正释放前的保留时间 |
| `WELL_POOL_MAX_MB` | `64` | 对象池内存上限（MB） |
| `WELL_RC_DEBUG` | 未设置 | `1` 时退出打印 live objects |
| `WELL_TYPE_DEBUG` | 未设置 | `1` 时退出打印类型元数据 |
| `WELL_SERVER_ID` | `0` | IdUtil serverId（0..1023） |

HTTP 常量（代码内，不是 env）：keep-alive 空闲 15s、响应超时 5s、header 16KB、body 1MB；WS 无空闲超时/背压。

---

## 10. 源码地图与扩展指引

```text
compiler/
  wellc/                 前端（lexer/parser/checker/codegen/driver）
  runtime/core/          运行时（wellrt/事件/Model/容器/线程/JSON/序列化/File/Timer）
  runtime/modules/http/  HTTP 服务器
  runtime/modules/ws/    WebSocket
  runtime/modules/mysql/ MySQL 客户端/池
  examples/              示例 .we
  tests/cases/           端到端用例
  tests/c/               C 单元/并发测试
  tools/                 verify.py / bench.py / bench server 模板
  docs/                  实现/语言文档
```

扩展路径：

- 加语法：`ast_nodes.py → parser.py → checker.py → codegen.py`，加测试用例；
- 加运行时能力：`runtime/core/wellrt.h` 声明 + 对应 C 文件 + `codegen.py` 的 builtin 分发；
- 加类型：`checker.check_type_name`、`codegen.ctype/is_ref`、`new_*/destroy_*` 生成；
- 改性能：优先看 bench（`tools/bench.py`）与生成 C；TCC 无优化，可用 `WELL_CC=gcc` 对比但需重新验证；
- 改 oid/TTL/内存语义属于**结构性变更**，先与项目 owner 确认并更新文档。

---

## 11. 验证与维护

### 11.1 测试格式

`tests/cases/<name>/`：

```text
main.we                  源码
expected.txt             期望 stdout（可选）
expect_compile_error.txt 期望编译错误子串（可选，非 0 退出）
expect_panic.txt         handler panic 的 stderr 子串（进程仍 exit 0）
expect_stderr.txt        stderr 子串（可配合 expect_exit.txt）
expect_exit.txt          期望退出码
rc_debug.txt             存在则设 WELL_RC_DEBUG=1 并匹配 "live objects at shutdown = 0"
http_port.txt / ws_port.txt / http_keepalive.txt / ws_concurrent.txt 等 网络测试标记
env.txt                  额外环境变量
```

- 输出比较会去掉行尾空格、首尾空行（保留中间空行）；
- 新增语言特性必须加用例；跑 `python tools/verify.py` 全绿再提交。

### 11.2 本文维护

- 本文描述当前实现；改动语言/运行时/默认值/性能基线时同步更新；
- 与 `IMPLEMENTATION.md`、`修改日志.md`、`待开发功能-优先级.md` 一起维护；
- 性能数字是 Windows 本机基线，换机器/编译器请重新生成 `tools/bench.py --save-baseline`，不要拿旧数字做跨环境断言。

---

## 附录：语法速览（非严格 EBNF）

```text
program      := (module | import | decl)*
decl         := const | enum | struct | event | controller | handler | func
              | namespace | static
struct       := "struct" ID ["extends" ID] "{" field* "}"
field        := ID ":" type ["=" expr] ";"
event        := "event" ID ["extends" ID] "{" field* "}"
controller   := "controller" ID "{" method* "}"
handler      := "handler" ID "{" method* "}"
method       := ID "(" params ")" [":" type] block
namespace    := "namespace" ID "{" func* "}"
static       := "static" ID "{" (field | func)* "}"
func         := ID "(" params ")" [":" type] block
const        := "const" ID ":" type "=" expr ";"
enum         := "enum" ID "{" (ID ["=" literal] ";")* "}"
stmt         := var | assign | if | while | for | foreach | switch
              | return | break | continue | block | expr ";"
var          := "var" ID [":" type] "=" expr ";"
switch       := "switch" "(" expr ")" "{" (case | default)* "}"
case         := "case" const-expr ":" stmt*      // 无 fallthrough
type         := "bool"|"int"|"long"|"float"|"double"|"String"|"Bytes"
              | "Array" "<" type ">" | "Map" "<" type ">" | "Linked" "<" type ">"
              | ID ("struct/event" 名)
entry        := "entry" ID ";"
```

---

*本文由项目维护者/AI 在 2026-10-22 基于当前实现编写；如发现与源码不一致，以源码与测试为准并更新本文。*
