# Well 语言与运行时（AI 参考）

> 面向需要生成/修改 Well 代码的 AI。只覆盖三部分：**语法规则**、**实现方式**、**注意事项**。
> 以当前实现为准（2026-10-22，115 个 `.we` 用例 + 12 个 C 测试通过）。冲突时以源码/测试为准。

---

## 1. 语法规则

### 1.0 命名规范（推荐，非强制）

编译器不强制命名，但推荐以下约定，便于跨项目阅读：

| 声明 | 规则 | 示例 |
|---|---|---|
| `struct` | 大驼峰（PascalCase）+ `Vo` 结尾 | `UserVo`、`OrderItemVo` |
| `event` | PascalCase + `Event` 结尾 | `UserEvent`、`PingEvent` |
| `controller` | PascalCase + `Controller` 结尾 | `UserController`、`AppController` |
| `handler` | PascalCase + `Handler` 结尾 | `UserHandler`、`PingHandler` |
| `enum` | PascalCase | `OrderStatus`、`Color` |
| `namespace` / `static` | PascalCase | `StringUtil`、`AppConfig` |
| 字段 / 方法 / 函数 / 局部变量 | lowerCamelCase（语言内置 API 也是此风格） | `userId`、`onEvent`、`handleOrder`、`itemCount` |
| 常量 | UPPER_SNAKE_CASE（推荐） | `MAX_RETRY` |

- 类型名首字母大写；`struct/event/controller/handler` 的固定后缀有助于区分角色；
- 字段/方法/变量使用小驼峰，是因为 `run`、`onEvent`、`Http.route`、`Model.get` 等语法/内置名固定，避免风格冲突；
- 内置 API、HTTP/WS 事件字段名（如 `reqId`、`clientId`、`method`、`path`、`query`、`body`）**不能改名**，它们是与运行时/协议约定的字段；
- 以上只是推荐，不通过编译检查。

示例：

```we
struct UserVo {
    id: long;
    name: String;
}

event UserEvent {
    userId: long;
}

handler UserHandler {
    onEvent(e: UserEvent) {
        print("user ", e.userId);
    }
}

controller UserController {
    run() {
        on(UserHandler);              // 推荐：从 onEvent 参数推断事件类型
        emit(new UserEvent(1) { userId: 7 });
    }
}

entry UserController;
```

### 1.1 文件与入口

```we
module demo.app;          // 解析后忽略
import "lib/util.we";     // 相对路径，声明合并
import lib.util;          // 点路径：lib/util.we 或 lib/util/index.we

entry MainController;     // 生成 main()，启动入口 controller
```

- `import` 的声明合并；同一文件重复声明报错；
- 必须有 `entry` 才能生成可执行入口。

### 1.2 类型

| 类型 | C/运行时表示 | 说明 |
|---|---|---|
| `bool` | `int` | `true/false` |
| `int` | `int`（32 位） | 超过 int32 的字面量自动按 `long` |
| `long` | `long long`（64 位） | |
| `float` / `double` | `double` | 两个名字都映射 double |
| `String` | `WellString*` | 不可变、引用类型、可 null；**长度/下标按字节** |
| `Bytes` | `WellBytes*` | 可变字节缓冲、引用类型 |
| `Array<T>` / `Map<T>` / `Linked<T>` | `WellArray* / WellMap* / WellLinked*` | 同质容器；Map 键为 String，保留插入序 |
| `struct` / `event` | `T*` | 引用类型、有 oid |
| `null` | `void*` | 所有引用类型可为 null |
| `void` | — | 返回类型/表达式 |

- 没有 enum 类型（enum 只是 int/String 常量）；没有 `char/short`；
- 所有引用类型默认 null；struct/event 的 String 字段默认 `""`。

### 1.3 声明

#### struct / event

```we
struct BaseVo {
    id: long;
}

struct UserVo extends BaseVo {      // 仅字段展平
    name: String;
    tags: Array<String>;
}

event BasePingEvent {
    n: int;
}

event PingEvent extends BasePingEvent {
    msg: String;
}
```

- `struct`/`event` 支持单继承**字段展平**：子类拥有基类全部字段；
- 根 `extends Vo` / `extends Event` 可省略、无实际作用；
- **没有方法继承、多态/子类型、虚调用**；`Derived` 不能赋给 `Base`；
- event 身份是子类自己的 `type_id`；`on(BaseEvent)` 不会收到 `DerivedEvent`，但可以显式 `on(DerivedEvent, Handler)`，前提是 handler 参数是 DerivedEvent 或其基类（见 §1.6）；
- 未知基类、循环继承、重复字段是编译错误；
- struct 没有用户方法，只有字段；
- 构造：
  - `new UserVo()`：`oid = ctx.oid`；
  - `new PingEvent(targetOid)`：事件 oid = targetOid；传 0 表示 emit 时生成新 oid；省略参数用 ctx.oid；
  - `new PingEvent(oid) { n: 1, msg: "x" }`：字段用 `:` 分隔；event 构造完成后冻结。

#### const / enum

```we
const MAX: int = 10;

enum Status { IDLE; RUNNING; DONE; }        // 0,1,2
enum Color { RED = "red"; BLUE = "blue"; }  // String 枚举

print(Status.fromInt(1));      // 1（未知 -> 0）
print(Status.toString(1));     // RUNNING（未知 -> ""）
print(Color.toString("red"));  // RED
```

- enum 成员用 `;` 分隔；int 枚举可显式赋值/自增；重复 int 值报错；
- `E.fromInt` 仅 int 枚举；String 枚举只有 `toString`。

#### controller

```we
controller MainController {
    run() {
        on(PingHandler);              // 推荐：省略 EventType
        Http.route("/hello", "GET", HelloHandler);
        Http.listen(8080);
    }
}
```

- 单例；`start(C)` 幂等，`stop(C)` 注销该 controller 的订阅并允许重启；
- `run()` 在事件循环前执行，`ctx.oid = 0`（装配期 oid）；
- 可以在 `run()` 里注册路由/订阅、初始化 static、创建 **oid=0 系统级只读数据**；
- `controller A extends B` 的父类逻辑不会执行（不支持继承，不要写）。

#### handler

```we
handler PingHandler {
    onEvent(e: PingEvent) {
        print(e.n);
    }
}
```

- 方法名也接受 `handle` / `on` / `handler`；
- 参数必须是事件类型；handler 无字段；
- `on(H)` 时事件类型从 `onEvent(e: T)` 推断。

#### func / namespace / static

```we
func add(a: int, b: int): int { return a + b; }

namespace Math {
    max(a: int, b: int): int { if (a > b) { return a; } return b; }
}

static AppConfig {
    limit: int = 10;
    title: String = "svc";
    arr: Array<int> = [1, 2, 3];

    bump(): int {
        limit = limit + 1;   // 仅启动期可写
        return limit;
    }
}
```

- `N.f(...)` 编译期解析为 static C 函数，零运行时开销、不占 oid/RC；
- static 不可实例化；字段启动期可写、运行期只读；
- static 引用字段不能是 event；运行期写/改容器/Bytes 会 panic；
- static 数据永不参与 TTL 回收，退出时统一 release；
- 启动期（controller `run`，ctx.oid=0）可写 static，含容器字面量赋值；
- 运行期需要可变状态请用 Model/oid。

### 1.4 语句

```we
var x: int = 1;          // 可省略类型
x = x + 1;

if (x > 0) { ... } else { ... }
while (x < 10) { x = x + 1; }
for (var i: int = 0; i < 3; i = i + 1) { ... }
for (v of arr) { ... }   // Array/Linked 值
for (k in map) { ... }   // Map 键（String）

switch (x) {
    case 1: print("one");
    case 2: print("two"); break;
    default: print("other");
}

return x;
break;
continue;
```

- switch 支持 `int/long/String`；**无 fallthrough**（每个 case 自动 break）；
- case 值必须是字面量/const/枚举成员，重复 case 报错；
- switch 也计入 breakable：`break` 退出 switch；`continue` 只作用于外层循环。

### 1.5 表达式

```we
1 + 2 * 3
a == b, a != b, a < b, a <= b, a > b, a >= b
a && b, a || b, !a
cond ? x : y
"a" + "b"              // 单次拼接 O(len(a)+len(b))；循环累积才 O(n²)，用 Bytes
arr[i], map["k"], obj.field
new UserVo(), new PingEvent(oid) { n: 1 }, new Bytes()
[1, 2, 3], {"k": 1}    // 容器字面量（受限，见下）
```

- 整数 `/`、`%` 生成 checked helper：除数为 0 → panic（带 file:line）；
- 字面量常量除零/常量溢出 → 编译错误；运行期整数溢出**没有语言级检查**（C 有符号溢出是 UB，不要依赖 wrap，见 §3.4.4）；
- 容器字面量 `[]`/`{}` 支持：`var` 初始化、下标赋值、static 字段默认值/启动期 static 赋值；
- **不能**把容器字面量直接当函数实参/普通 struct 初始化字段，先赋给变量；
- 没有闭包/lambda/用户泛型/接口/元组。

### 1.6 事件与订阅

```we
on(PingHandler);                // 推荐：从 onEvent 推断事件类型，channel 0
on(PingHandler, 7);             // 推荐 + 指定 channel
once(PingHandler);              // 至多一次
off(PingHandler);               // 注销（按 owner）

// 也可显式写事件类型（等价，事件类型必须匹配）
on(PingEvent, PingHandler);
on(PingEvent, PingHandler, 7);

emit(new PingEvent(123) { n: 1 }); // 目标 oid=123
emit(ev, 7);                    // 指定 channel
start(ChildController);
stop(ChildController);
```

- **推荐 `on(H)` / `once(H)` / `off(H)`**：从 handler 的 `onEvent(e: T)` 参数自动推断事件类型，少写重复的 EventType；
- 也支持显式 `on(EventType, H[, channel])`：EventType 可以是 handler 参数事件类型本身，**也可以是它的子类**（事件字段展平，handler 只能安全访问基类字段）；
- 反过来（EventType 是基类、handler 参数是子类）不允许；`on(H)` 推断的是 handler 参数类型本身，想订阅子类事件要写显式形式；
- 例：

```we
event BasePingEvent {
    n: int;
}
event PingEvent extends BasePingEvent {
    msg: String;
}

handler PingHandler {
    onEvent(e: BasePingEvent) {
        print(e.n);            // 只能访问基类字段
    }
}

on(PingEvent, PingHandler);    // 允许：PingEvent 继承 BasePingEvent
```
- channel 是 long：0 默认、>0 普通、`ALL` 只用于订阅；
- emit 是异步派发；event 的 oid 是调度目标：`new PingEvent(0)` 的 oid 为 0，`emit` 时用 `IdUtil` 生成一个新的非 0 oid；`new PingEvent(targetOid)` 则直接进入 targetOid 的队列，`emit` 不改写它的 oid；
- 同一 oid 事件 FIFO 串行；不同 oid 可并行；
- `once` 在订阅锁内 claim，至多执行一次；`off/stop` 置 inactive。

### 1.7 内建 API（速查）

```text
print(...)                        逐个打印 + 换行（不自动加空格）
Model.get(T, oid)                 Cache -> Store -> Cache，owned T*（可 null）
Model.peek(T, oid)                只查 Cache，owned T*（可 null）
Model.set(obj)                    要求 obj.oid == ctx.oid；写 Cache + Store
Model.remove(T, oid)              要求 oid == ctx.oid；Cache + Store 删除
Cache.get(T, oid)                 只查 Cache，owned T*（可 null）
Cache.set(obj)                    只写 Cache，要求 obj.oid == ctx.oid
Cache.remove()                    删除当前 ctx.oid 的全部 Cache 行（所有类型）

IdUtil.getId()                    long（雪花式 id）
Json.parse(T, String)             T*，失败 panic
Json.tryParse(T, String)          T* 或 null，不 panic
Json.stringify(v[, indent])       String；v 可为 struct/event/null/标量/顶层 Array/Map/Linked
File.read/write/exists/remove(path[, text])
Host.nowMs() / Host.env(name) / Host.log(...)
Http.route(path, method, Handler) / Http.listen(port) / Http.respond(reqId,status,body) / Http.stop()
Ws.route(path, OpenH, MsgH, CloseH) / Ws.listen(port) / Ws.send(clientId,text) / Ws.close(...) / Ws.stop()
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

- 越界读返回值类型默认值（引用 null）；Array 越界写会扩展；
- 容器写要求 `container.oid == ctx.oid`；插入引用元素要求 `element.oid == container.oid`（String 例外）；
- 不支持 `Map.entries`。

### 1.8 错误模型

- 编译错误：`file:line:col: message`，非 0 退出；
- handler panic：打印 `well panic: file:line: message`，**只终止当前任务**，调度器继续；
- controller/装配期 panic：未捕获，**exit 70**；
- 没有 try/catch / Result / 异常；可恢复解析用 `Json.tryParse`；
- `File.*`、`Json.parse`、整数除零、oid 违规等是 panic。

---

## 2. 实现方式

### 2.1 编译流水线

```text
.we
 → Python 前端：lexer → parser → checker → codegen
 → build/<name>.c
 → C 编译器（默认仓库内 TinyCC；可用 WELL_CC=gcc/clang，自动 -O2）
 → 可执行文件（链接 runtime/core + 用到的 modules）
```

- codegen 为每个 struct/event 生成：`WellObj hdr` + 字段、`T_<name>` type_id 宏、`new_*`/`destroy_*`；
- handler 生成 `H_onEvent(WellCtx*, void*)`；controller 生成 `ensure/run/stop`；func/namespace 生成 `static` C 函数；
- 每个语句前生成 `well_source_set(file,line)`，用于 panic 定位；
- 只编译程序实际用到的 `Http/Ws/Mysql` 模块源文件；
- `WELL_CC` 指定编译器；gcc/clang 默认 `-O2`，`WELL_CFLAGS` 可覆盖（空串禁用自动 -O2）；TCC 使用默认参数。

### 2.2 对象模型与引用计数

- 每个对象以 `WellObj` 开头：`rc`、`type_id`、`flags`、`oid`、registry 指针、`alloc_size`（x64 约 56B）；
- RC 是原子计数；`new` 为 1；字段赋值/Model 引用会增加，释放减少；
- `WellCtx` 是 handler 帧日志：内联 16 个 refs + 4 个 pins，统一 release；
- 允许成环；环不立即回收，最终由 oid 过期回收兜底；
- `WELL_RC_DEBUG=1` 退出时打印 live objects，正常应为 0。

### 2.3 oid 与所有权

- oid 是数据所有者：
  - `new UserVo()` → `oid = ctx.oid`；
  - `new UserEvent(target)` → `oid = target`；target 0 在 emit 时生成新 oid；
- 字段写：`target.oid == ctx.oid`；引用值写：`value.oid == target.oid`（String 例外、null 允许）；
- 容器写：`container.oid == ctx.oid`；插入引用元素：`element.oid == container.oid`（String 例外）；
- event 构造字段豁免 oid 检查（可携带异 oid 只读引用），构造完成后冻结；
- Model/Cache 写要求对象/oid 与 ctx.oid 相等；
- controller `run` 的 ctx.oid=0：这里 `new` + `Model.set` 得到 **oid=0 系统级只读数据**；handler 可读，写会 panic，永不 TTL 回收；
- 装配期 oid（当前 0）永不参与过期回收。

### 2.4 调度器

- 每 oid 一个 FIFO 队列；同 oid 同时最多一个 runner；异 oid 可并行；
- worker 数：`WELL_WORKERS` 未设置/`auto` → `max(1, min(64, cores*3/4))`（8 核→6，留 CPU 给 I/O 线程/OS）；显式 1..64 覆盖；主线程 id=0 也参与；
- per-worker local deque + work stealing；事件/任务节点和队列有对象池；
- `well_run_loop` 在 `pending==0 && service_count==0` 时退出；HTTP/WS/Timer 通过 service 计数保持进程存活；
- event 派发期间对 target oid 执行 `well_oid_pin`，所有 handler 执行完、event 释放后 `well_oid_unpin`；handler 即使不做 `Model.get/set`，执行期间也不会被过期回收；
- handler 调用有 setjmp/longjmp 边界，panic 隔离；controller/装配期 panic 不隔离，exit 70。

### 2.5 Model / Cache / Store

```text
Model.get(T, oid):   Cache hit -> owned
                     miss -> Store.select(编码字节) -> decode -> Cache.set -> owned
Model.peek(T, oid):  只查 Cache
Model.set(obj):      Store.upsert(编码) -> Cache.set
Model.remove(T,oid): Store.delete -> Cache.remove
Cache.get/set:       只操作 Cache，不经过 Store
Cache.remove():      删除当前 ctx.oid 的全部 Cache 行（所有类型）
```

- Cache：64 分片哈希表 + spinlock；key=(type_id, oid)；
- Store：接口；默认 MemoryStore 只存编码字节；进程退出即丢；没有自动磁盘持久化；
- Cache 与 Model 共用同一全局 Cache；
- **手动释放**：oid 完成工作后调用 `Cache.remove()`；它只删 Cache 行，Store 不变；被 handler/event 仍持有的对象在引用释放后销毁；
- TTL 默认 60s（`WELL_MODEL_TTL_MS`，0 关闭）：oid 空闲 + `exec_ref==0` + 调度器空闲时，整组回收该 oid 数据；TTL 是**最后保底**，不是主要释放手段；
- 清理是隔离 + 分批（`WELL_MODEL_TTL_BATCH`，默认 64）：短锁摘元数据，锁外释放对象，不阻塞其他 oid。

### 2.6 内存

- 对象池：`well_new` 按 16B size class 复用，分配按 class 整块大小；`WELL_POOL_TTL_MS` 默认 60s，`WELL_POOL_MAX_MB` 默认 64MB；
- String 用 malloc；struct/event/容器走池；Bytes 分配走池但 destroy 直接 free；
- Cache 存对象引用；Store 另存编码字节（默认 MemoryStore），Model 数据有“对象 + 字节”两份；
- 局部引用在 loop/block/return 等帧边界释放。

### 2.7 static / 装配期

- 启动顺序：运行时 init → 类型注册 → static owner oid 设置（当前 0）→ static init → 模块 init → controller run → `well_run_loop`；
- `well_run_loop` 开始时 `well_static_freeze()`：static 从可写变只读；
- static 值要求 `value.oid == 装配期 oid`（String 例外）；event 不允许放进 static；
- static 容器/Bytes 原地变异被 oid 检查拦截；
- static 数据永不 TTL 回收，退出统一 release。

### 2.8 HTTP / WebSocket / Timer / MySQL（实现要点）

- HTTP：listener + 每连接 reader 线程；keep-alive（空闲 15s 关闭）；每请求 task 在 worker 上执行；`Http.respond` 首个响应生效，5s 无响应 504；header 16KB/body 1MB；无 TLS/chunked/pipelining/连接数上限；
- WS：listener + 每连接 reader 线程；每连接一个 oid，open/message/close task 串行；text/ping/pong/close；无 TLS/分片/二进制/背压；
- Timer：独立线程约 5ms 轮询；到期以 timer id 为 oid 投递 task；有 timer 时进程保持运行；
- MySQL：自实现 wire protocol，native auth + COM_QUERY；连接池；同步调用。

---

## 3. 注意事项

### 3.1 oid / Model / Cache

1. **不要跨 oid 引用业务对象**：普通字段/容器会 panic `well oid violation`；
2. `Model.get`/`Cache.get` 返回 owned；交给变量/帧日志，不要手工 free；
3. 不要把 Model 对象裸指针长期保存在 C 全局/自定义 C 模块；C/FFI 跨调用保存需 `well_retain` + `well_oid_pin`，用完 `well_oid_unpin` + `well_release`；
4. oid 完成工作后主动 `Cache.remove()`；TTL 只是保底；
5. controller `run` 里 `new` + `Model.set` 创建的是 **oid=0 系统级只读数据**：可读、不可写、永不 TTL 回收；可写数据放事件 handler；
6. 不要用 `Model.peek` 做“只读探测”——它会刷新 TTL（等于保活）；
7. `Cache.set` 只写 Cache，不写 Store；需要持久化用 `Model.set`。

### 3.2 事件 / static

1. event 构造后冻结，字段只读；不要存到 static/跨 oid 结构；
2. `emit` 对 **oid==0** 的 event 会先分配一个新 oid（`IdUtil`），再入队；非 0 oid 的 event（`new PingEvent(targetOid)`）不会被改写。因此 oid=0 的 event 要到 emit 时才会得到最终目标 oid；不要把它放进 static/装配期数据；
3. static 只放配置/只读数据；不要尝试运行期写；
4. `controller`/`handler` 不支持继承，父类逻辑不会执行；`struct`/`event` 只支持字段展平，无多态。

### 3.3 handler / 阻塞

1. handler 是 run-to-completion：没有 await/yield；阻塞 I/O 会卡住该 oid 的 runner 和一个 worker；
2. 重 CPU/阻塞操作考虑拆 oid、加 `WELL_WORKERS` 或改用批量/异步方案；
3. handler 内 panic 被隔离；controller 内 panic 会 exit 70。

### 3.4 语法限制与已知边界（按实际状态）

#### 3.4.1 未实现（功能缺口，不是 bug）

- 无异常 / `try-catch` / `Result`；错误处理是 panic + 可空 API（见 §1.8）；
- 无用户泛型 / 接口 / 元组 / 闭包 / lambda；函数不是一等值，不能捕获局部变量；
- 无 `Map.entries`；可用 `keys()` / `values()` / `for-in`；entries 需要键值对/tuple 类型设计；
- 无 `char/short`；整数只有 `int/long`，`float` 是 `double` 别名；
- 容器字面量不能直接当函数实参/普通 struct 初始化字段；当前只支持 `var` 初始化、下标赋值、static 字段默认值/启动期 static 赋值；
- 无 async/await/协程；handler 是 run-to-completion。

#### 3.4.2 有意语义（设计选择，不是待修 bug）

- switch **无 fallthrough**，case 值必须常量；
- String 的 `length`/下标/切片按**字节**；UTF-8 用 `codePointAt` / `fromCodePoint`；单次 `a + b` 是 O(n+m) 分配+复制，正常；但循环 `s = s + x` 累计是 O(n²)，不要当 builder 用；大量拼接用 `Bytes`（本机 20k 次追加约 0.6s，Bytes 约 1–2ms）；
- `Json.parse` 失败 **panic**；需要可恢复解析用 `Json.tryParse` 返回 `null`；
- 整数 `/`、`%` 除零 **panic**（带 file:line）；字面量常量除零/溢出是编译错误。

#### 3.4.3 已处理/已有替代

- 整数除零/取模零：checked helper，panic 带源码位置；
- 字面量常量除零、常量算术溢出：编译错误；
- 超过 int32 的整数字面量自动按 `long`；
- `Json.tryParse`、`Json.stringify`、`String.codePointAt/fromCodePoint`、`Bytes` 增量拼接均已提供。

#### 3.4.4 已知风险：运行期整数溢出未检查

- 生成 C 的 `+ - *` 是有符号运算，运行期溢出**没有语言级检查**；
- TCC 下通常表现为 wrap，但 C 有符号溢出是 **UB**：gcc/clang `-O2` 下不能把 wrap 当作可靠保证，优化可能改变结果；
- 需要确定性时：避免依赖溢出、显式范围检查；或后续加 checked 模式 / `-fwrapv`（当前默认都没有）；
- 这不是“已修复”，而是明确的已知边界。

### 3.5 配置/验证

```powershell
python wellc.py build app.we -o build\app.exe
python tools\verify.py --quick    # 115 个 .we 用例
python tools\verify.py            # + 12 个 C 测试
```

- `WELL_WORKERS` 未设置时按 CPU 自动；测试 runner 固定 1；
- `WELL_CC=gcc` 使用 gcc（自动 -O2）；`WELL_CFLAGS` 覆盖；
- 生成的 C 在 `build/<name>.c`，是排查 codegen 问题的第一手材料。
