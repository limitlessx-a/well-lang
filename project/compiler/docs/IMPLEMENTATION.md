# Well MVP 编译器实现说明

> 状态：可工作的 MVP。`.we` → Python 前端 → C99 → 本机可执行文件。
> 目标：把设计稿中"oid 单写者 / 写入点校验 / 事件 / Model / 引用计数 A 方案"跑通，而不是实现完整语言。

---

## 1. 目录与管线

```
.we 源码
  │  lexer.py        token
  │  parser.py       AST
  │  checker.py      符号表 + 类型检查 + 事件绑定校验
  │  codegen.py      C99
  ▼
build/<stem>_<sha1前8>.c  ──(TCC / gcc / clang)──►  <stem>_<sha1前8>.exe
                         │
                  runtime/core/wellrt.c
                  runtime/core/well_model.c
                  runtime/core/well_containers.c
                  runtime/core/well_event.c
```

| 文件 | 职责 |
|---|---|
| `wellc.py` | CLI：`build` / `run` / `emit-c` |
| `wellc/lexer.py` | 词法（C 风格注释、字符串、数字、关键字） |
| `wellc/parser.py` | 递归下降语法分析 |
| `wellc/ast_nodes.py` | AST 定义 |
| `wellc/checker.py` | 声明收集、字段展平、表达式类型标注、事件名绑定校验 |
| `wellc/codegen.py` | C99 生成（对象构造/析构、handler 包装、controller run、main） |
| `wellc/driver.py` | import 合并、调用 C 编译器、运行 |
| `runtime/core/wellrt.[ch]` | 基础运行时：原子 RC、对象、Ctx 帧日志、oid 检查、String、print、init/shutdown |
| `runtime/core/well_model.[ch]` | 64 分片 Model 哈希表 + spinlock |
| `runtime/core/well_model.c` | Model facade：Cache + Store 组合 |
| `runtime/core/well_cache.c` | 64 分片内存 Cache |
| `runtime/core/well_store.c` | Store 接口 + MemoryStore |
| `runtime/core/well_containers.[ch]` | `Array<T>` / `Map<T>` / `Linked<T>` |
| `runtime/core/well_event.[ch]` | 订阅表、每 oid 队列、worker 线程池、panic 隔离 |
| `runtime/core/well_serialize.c` | 元数据驱动的行级二进制序列化 |
| `runtime/core/well_json.c` | 元数据驱动的 JSON 解析 |
| `runtime/core/well_file.c` | File 与 Host API |
| `runtime/modules/http/well_http.c` | HTTP/1.1 最小服务器 |
| `runtime/modules/ws/well_ws.c` | WebSocket 握手 + frame + 路由 |
| `runtime/modules/mysql/well_mysql.c` | 最小 MySQL 客户端（native password + COM_QUERY） |
| `runtime/core/well_thread.[ch]` | 平台线程/互斥量/条件变量封装（Windows / pthread） |
| `runtime/core/well_internal.h` | 模块间内部接口（容器注册、Model shutdown、panic 边界） |
| `examples/` | 示例 |
| `tests/run_tests.py` | 端到端测试（编译 + 运行 + 期望输出/错误） |

---

## 2. 已实现的语言子集

### 2.1 声明

| 语法 | 状态 | 说明 |
|---|---|---|
| `module a.b;` | ✅ 解析忽略 | 单文件/import 合并时不参与命名 |
| `import "relative.we";` / `import a.b;` | ✅ | 本地相对/点路径解析，decl 合并 |
| `entry Controller;` | ✅ | 生成 `main()`，调用 `ctl_<Controller>_ensure`（幂等启动） |
| `const N: int = 1;` | ✅ | 生成 C 宏；值必须是编译期常量（字面量/枚举成员/其它 const/它们的数值运算），`new`/函数调用/Host 读取/String 拼接会在 check 阶段报错 |
| `enum E { A = 1; B; }` | ✅ | int 自增 |
| `enum E { A = "A"; }` | ✅ | String 枚举，成员生成 `well_string_lit(...)`；可作值/switch case（普通常量，不承担事件名）；`enum E: String` 声明类型会校验成员与基类型一致 |
| `E.fromInt(v)` / `E.toString(v)` | ✅ | int 枚举：整数→枚举值（未知→0）、值→成员名 String（未知→""）；String 枚举只支持 `toString`（值→成员名） |
| `struct S extends Base { f: T; }` | ✅ | 继承字段展平（无多态）；`extends Base` 可省略，默认 `Vo` |
| `event E extends Event { ... }` | ✅ | 事件类型即身份；无 `bind`，无名字注册表；`extends Event` 可省略，默认 `Event` |
| `controller C { run(){...} }` | ✅ | 单例装配器；`start(C)` 幂等，无 `start(C, oid)`；**只允许 `run()`**（其它方法编译错误），必须定义 `run()`；不支持 `extends`（语法已移除） |
| `handler H { onEvent(e:E){...} }` | ✅ | 只允许一个事件方法，方法名可为 `onEvent`/`handle`/`on`/`handler`；参数必须是**具体 event 类型**（根 `Event` 报错）；不支持 `extends`（语法已移除） |
| `func f(a:int):int { ... }` | ✅ | 生成带隐藏 `WellCtx*` 的 C 函数 |
| `namespace N { f(a:int):int {...} }` | ✅ | 静态函数集合；`N.f(...)` 编译期解析，生成 `static wf_N__f(WellCtx*, ...)`，零运行时开销、不占 oid/RC |
| `static S { f: int = 1; m(): int {...} }` | ✅ | 静态类：**启动期可写、运行期只读**的静态字段 + 静态函数，不可实例化；`S.f` / `S.m()` 编译期解析 |

**声明唯一性**：同文件重复声明报错；跨文件同名（任意类别：类型/函数/controller/handler/enum/const/namespace/static）同样报错（全局单命名空间，错误信息带首次定义文件）；参数重名、同作用域 `var` 重名报错；内层作用域可遮蔽外层。

**字段默认值**（struct/event 字段与 static 字段同一套规则）：只允许字面量/const、容器字面量、`new Bytes()` / `new Array<T>()` / `new Map<T>()`；函数调用、`new <struct>`、Host 读取等在 check 阶段报错。

`namespace` 语义（2026-10-22 加入）：
- 成员只能是函数（暂不放 const/enum/struct）；函数可省略返回类型（视为 void）；
- 调用形式 `N.f(args)`；namespace 内部可**按名字直接调用同 namespace 的其他函数**；
- 不是一等值：`N` 不能赋值/传参/作为表达式；
- 不支持嵌套 namespace、别名/using、函数重载；
- C 名字修饰 `N__f`，与顶层 `func` 不冲突；`import` 后可直接使用。

`static` 语义（2026-10-22 加入，用于 config/工具类）：
- 成员：`name: Type [= 默认值];` 字段 + 函数；字段可写（`S.f = expr`），函数可读写 `S.f`；
- **不可实例化**：`new S()`、`var x: S` 都是编译错误（`static S cannot be used as a value type`）；
- 字段默认值支持：字面量/const、容器字面量（`arr: Array<int> = [1,2,3];`、`m: Map<int> = {"k":1};`）、`new Bytes()/new Array<T>()/new Map<T>()`；标量直接作为 C 全局初始化，其余在 `well_statics_init()` 中用装配期 oid 的信任上下文（`well_static_owner_oid()`，当前 0）构造一次（对象 oid = 装配期 oid，后续只读）；其余值在**启动期**（controller `run`，ctx.oid == 装配期 oid）赋值；
- 启动期支持**容器字面量赋值**：`Cfg.arr = [1,2,3];` / `Cfg.m = {"k":1};`（codegen 先构造临时容器再转移所有权给 static；运行期写会先被只读检查拦截，不会构造临时对象）；
- 引用字段赋值走 `well_static_set_ref`：释放旧值、retain 新值（owned 值转移所有权）；进程退出前 `well_statics_shutdown()` 统一 release，保证 `WELL_RC_DEBUG` 干净；
- 字段没有 oid，**不参与 oid 写检查**；C 名字修饰 `S__field` / `S__f`；
- **运行期只读（已强制）**：所有 static 字段（引用与标量）仅在启动期（`well_run_loop` 之前，且 `ctx.oid == 装配期 oid`）可写；`well_run_loop` 启动时 `well_static_freeze()` 冻结；运行期写会 panic `static field S.f is read-only (assembly oid=..., ctx oid=...)`。
- 引用字段的值必须与装配期 oid 一致（`value.oid == ctx.oid`，String 无 oid 例外），防止把运行期 oid 的业务对象塞进 static。
- **引用字段类型不能出现 event**（直接声明、容器元素、或经 struct 字段间接可达都会被编译期拒绝）：`emit` 会给 oid=0 的 event 分配新的非 0 oid（`IdUtil`），static 持有的 event 会因此变成非装配期 oid、可能被 TTL 清理；需要事件对象请在 handler 内局部创建。
- static 容器/Bytes 的**原地变异**也被 oid 检查拦截（对象 oid=0 vs handler oid≠0：`Array.push/sort/reverse`、`Bytes.*`、`Map/Linked` 写操作）。
- static 数据（装配期 oid，当前 0）**不参与自动 TTL 清理**（按装配期 oid 豁免），进程退出时统一 release；需要运行期可变状态请用 Model/oid。
- namespace 内的函数在启动期可读写 `S.f`，运行期同样只读。

### 2.2 类型

`bool / int / long / float / double / String / Bytes / void / null`、用户 struct/event，以及 `Array<T>` / `Map<T>` / `Linked<T>`（同质；`Map` 键为 String）。
- `String` 是引用类型（`WellString*`），默认 `""`，不可变，字面量驻留；长度与下标都按 **字节**（与 `String.length` 一致）；字节长度缓存在 `WellString.len`，`s.length` / `s[i]` / `trim/startsWith/endsWith/split` 的长度计算为 O(1)（解析循环里不会退化成 O(n²)）。
- `Bytes` 是可变字节缓冲（`WellBytes*`），`new Bytes()` 创建；用于增量拼装文本/二进制负载（避免**循环累积** `String +` 的 O(n²)；单次 `a+b` 是 O(len(a)+len(b))）。**写操作参与 oid/FROZEN 检查**（`push/set/append/appendStr/clear` 与容器写一致）：handler 里不能修改启动期创建（oid=0）或其它 oid 的 Bytes，会 panic `well oid violation`。
- `int`/`bool` → C `int`；`long` → `long long`；`double`/`float` → `double`；`Bytes` → `WellBytes*`。
- `Array<T>` → `WellArray*`；`Map<T>` → `WellMap*`；`Linked<T>` → `WellLinked*`；三者是引用类型，带 oid。
- 所有引用类型可为 `null`。
- **赋值兼容性**：同类型直接允许；数值只允许 widening——`numeric_rank` 为 `bool=int=0 < long=1 < float=double=2`，即 `bool→int`、`int→long`、`int/long→double` 可赋值，窄化（`long→int`、`double→int`、`double→long`）报 `cannot assign ... (narrowing conversion is not allowed)`；
- `null` 只能赋给引用类型（String/Bytes/Array/Map/Linked/struct/event），赋给数值类型报 `cannot assign null to ...`；
- `void` 只能作返回类型（`check_type_name` 的 `allow_void`），作为局部变量/字段/参数/表达式值都报错；
- 带注解 `var` 必须与初始值类型兼容；用户函数/方法的实参逐项用 `check_assignable` 校验（不再只看个数）。

### 2.3 语句

`var`（可带类型）、赋值、`if/else`、`while`、`for(init;cond;step)`、`for (var x of array)`、`for (var k in map)`、`switch/case/default`、`return`、`break`、`continue`、块。

- `for-of`/`for-in` 的循环变量必须有 `var`（`for (var x of a)`），`for-of`/`for-in` 内 `break`/`continue` 正常。
- `switch (expr) { case <常量>: 语句...; default: 语句...; }`：expr 支持 `int/long/String`；**无 fallthrough**（每个 case 自动 break）；case 值必须是字面量/const/枚举成员且不能重复；`break` 在 switch 内表示退出 switch（switch 也计入 breakable 结构），`continue` 只作用于外层循环；String switch 生成 `well_string_eq` 的 if/else 链（String 枚举成员可作 case）。

### 2.4 表达式

字面量、标识符、成员访问、下标 `a[i]` / `m["k"]` / `s[i]`（String 下标返回字节值 `int`，越界 `-1`）、调用、`new T(...)`、对象初始化 `{ f: v }`、数组字面量 `[...]`、Map 字面量 `{ "k": v }`、算术/比较/逻辑、三元、字符串拼接、`String.length`。

- **整数除法/取模**：`int`/`long` 上的 `/`、`%` 生成 `well_int_div` / `well_int_mod`；除数为 0 时 panic `integer division/modulo by zero`（带 `file:line`），不再触发 `0xC0000094` 崩溃；
- **编译期常量检查**：字面量除数/模数为 0 直接编译错误；只含整数字面量的常量算术表达式在编译期检查 int32/`long` 64 位溢出（`integer constant overflow`）；
- 超出 int32 的整数字面量自动按 `long` 处理；运行期整数溢出语义为**二补码 wrap**：gcc/clang 构建始终追加 `-fwrapv`，TCC 代码生成本身即按 wrap 实现。
- **数值字面量**：十进制（`08` 按 8 处理）与 `0x` 十六进制；空 hex（`0x`）、指数缺数字（`1e`）、非有限 float（`float literal out of range`）报词法/语法错误；字符串未知转义（如 `\q`）报错。
- `??` **未实现**：checker 对 `??` 报 ``operator ?? is not implemented; write `x == null ? y : x` instead``，用三元代替。

String 方法（全部字节语义）：

| 调用 | 语义 |
|---|---|
| `s.byteAt(i)` / `s.charCodeAt(i)` / `s[i]` | 第 i 个字节的无符号值；越界 `-1` |
| `s.codePointAt(i)` | 从字节下标 i 解码一个 UTF-8 码点；越界/非法序列返回 `-1` |
| `s.substring(a, b)` / `s.slice(a, b)` | 区间 `[a, b)`；越界自动 clamp |
| `s.indexOf(x)` / `s.indexOf(x, from)` | 子串位置，未找到 `-1` |
| `s.contains(x)` / `s.startsWith(x)` / `s.endsWith(x)` | `bool` |
| `s.trim()` | 去首尾 ASCII 空白，返回新 String；无空白时直接返回原引用（retain，不复制） |
| `s.toLower()` / `s.toUpper()` | ASCII 大小写转换，返回新 String |
| `s.replace(old, new)` | 全部替换（非重叠、从左到右）；`old` 不能为空；未命中时共享原串（retain） |
| `s.split(sep)` | `Array<String>`；`sep` 为空串时不切分 |
| `String.fromByte(n)` | 取低 8 位构造单字节 String |
| `String.fromCodePoint(cp)` | UTF-8 编码一个码点；非法码点（含代理区）→ U+FFFD |
| `String.fromInt(n)` | 整数转十进制字符串（`long`/`int`/`bool`） |

Bytes（可变字节缓冲）：

| 调用 | 语义 |
|---|---|
| `new Bytes()` | 空缓冲 |
| `b.length` | 字节数（`long`） |
| `b.push(v)` / `b.get(i)` / `b.byteAt(i)` / `b[i]` | 追加一个字节 / 读取字节；越界 `-1` |
| `b.set(i, v)` | 写入字节；越界 panic |
| `b.append(other)` / `b.appendStr(text)` | 追加另一个 Bytes / 一个 String |
| `b.clear()` | 清空（保留容量） |
| `b.toString()` | 复制为 String（按文本处理，二进制负载应保持 Bytes） |

临时值说明：方法返回的 String/Bytes 作为临时值参与链式表达式（`.length`、下标、`==`、`+`、再调用 String 方法）时会自动释放；赋给变量后按普通引用计数管理。

### 2.5 内建调用

| 调用 | 语义 |
|---|---|
| `print(...)` | 逐个打印后换行（无自动空格） |
| `Model.get(T, oid)` | Cache → Store → Cache；返回 `T*`（owned）；未命中 `null` |
| `Model.peek(T, oid)` | 只查 Cache；返回 `T*`（owned）；未命中 `null` |
| `Model.findByOid(T, oid)` | `get` 的别名/兼容接口；语义同 read-through |
| `Model.set(vo)` | 写入/替换，要求 `vo.oid == ctx.oid`；重复 set 幂等 |
| `Model.remove(T, oid)` | 要求 `oid == ctx.oid` |
| `Cache.get(T, oid)` | 只查 Cache，返回 `T*`（owned）；不访问 Store |
| `Cache.set(vo)` | 只写 Cache，要求 `vo.oid == ctx.oid`；不写 Store |
| `Cache.remove()` | 删除当前 `ctx.oid` 的全部 Cache 行（所有类型）；Store 不变 |
| `on(EventType, H)` / `once` / `off` | 订阅 channel 0 |
| `on(H)` / `once(H)` / `off(H)` | **省略 EventType**：从 handler 的 `onEvent(e: T)` 参数推断事件类型，订阅 channel 0 |
| `on(EventType, H, channel)` | 订阅指定 channel（`long`）；`ALL` 为通配 |
| `on(H, channel)` | 省略 EventType + 指定 channel（二参时若首参是 handler 名，则视为 `(H, channel)`） |
| `emit(event)` | 发布到 channel 0 |
| `emit(event, channel)` | 发布到指定 channel（`long`，不能是 `ALL`） |
| `start(C)` | 单例启动；重复调用幂等（只执行一次 `run`） |
| `stop(C)` | 注销该 controller 类型注册的订阅，重置 started，允许再次 `start` |
| `IdUtil.getId()` | 返回 `long`：`serverId + timestamp + sequence`，线程安全；`WELL_SERVER_ID` 0..1023 |
| `Json.parse(Type, String)` | 按元数据解析 JSON 为 struct/event；嵌套对象、容器、ref、字符串转义/Unicode；失败 panic |
| `Json.tryParse(Type, String)` | 同上但**失败返回 `null`**（不 panic），部分构造的根对象会被释放 |
| `Json.stringify(value[, indent])` | 序列化为 JSON String：支持 struct/event、null、标量（int/long/double/bool/String）、顶层 `Array<T>/Map<T>/Linked<T>`；嵌套容器用字段元数据或顶层静态元素类型；String 转义、非有限 double→null、深度 >64 输出 null；`indent` 0（默认紧凑）或 1–8 空格 pretty |
| `File.read/write/exists/remove` | 文件读写/存在/删除；失败 panic |
| `Host.nowMs/env/log` | 当前毫秒时间、环境变量、stderr 日志 |
| `Http.route/listen/respond/stop` | HTTP/1.1 服务器：多连接、keep-alive、handler 在 worker 上异步响应 |
| `Ws.route/listen/send/close/stop` | WebSocket 独立端口、握手、text frame |
| `Timer.once(ms, Handler)` / `Timer.loop(ms, Handler)` / `Timer.cancel(id)` | 定时器：到期把 handler 作为 task 投递到 timer id 的 oid；返回 timer id |
| `Mysql.connect/exec/queryJson/close` | 最小 MySQL 客户端；native auth + COM_QUERY |
| `new Array<T>()` / `new Map<T>()` / `new Linked<T>()` | 空容器；Array/Map 也可用 `[]` / `{}` 字面量 |
| `l.pushHead(v)` / `l.pushTail(v)` / `l.popHead()` / `l.popTail()` | Linked 首尾增删 O(1) |
| `l.head()` / `l.tail()` / `l.get(i)` / `l.length` | 查看首尾、按索引查找 O(n)、长度 |
| `a[i]` / `a[i] = v` | Array 下标读写；越界读返回值类型默认值（引用 `null`），越界写扩展 |
| `a.get(i)` / `a.set(i, v)` | 显式读写，语义同下标 |
| `a.length` / `a.push(v)` / `a.pop()` | 长度、尾部追加、尾部弹出 |
| `m["k"]` / `m["k"] = v` | Map 读写；缺失返回值类型默认值（引用 `null`） |
| `m.get(k)` / `m.set(k, v)` | 显式读写，语义同下标 |
| `m.size` / `m.has(k)` / `m.delete(k)` | 大小、键判断、删除 |
| `for (var x of a)` / `for (var k in m)` | Array 值迭代 / Map 键迭代 |

---

## 3. 运行时设计

### 3.1 对象头

```c
typedef struct WellObj {
    long long oid;
    void *exec_pins;
    struct WellObj *reg_next;
    struct WellObj *reg_prev;
    WellAtomicInt rc;
    unsigned int type_id;
    unsigned int flags;
    unsigned int alloc_size;   /* 32-bit: pool class / allocation size */
} WellObj;                     /* 48 bytes (8-byte fields first, no padding) */
```

每个 struct/event 的第一个字段是 `WellObj hdr`；`String` 是 `{ WellObj hdr; unsigned long long hash; long long len; }`，字节数据始终内联在 header 之后，用 `well_str_data(s)`（即 `(char *)(s + 1)`）访问。
`WellObj` 为 48B、`WellString` 为 64B、`WellBytes` 为 72B（v1：56B/80B/80B）。`alloc_size` 是 32 位；
大于 `POOL_MAX_SIZE` 的对象记 0（释放时直接 `free`），因此不需要 64 位大小字段。

`WellString` 的 header 与字节数据在**同一块 malloc / size-class 池块**中（字节紧跟在 header 之后，`well_str_data(s)` 访问），减少一次分配/释放；`hash` 在首次用于 Map key/相等比较时惰性计算（`WELL_FLAG_HASHED`），空操作数拼接（`"" + b`、`a + ""`）直接 retain 另一侧，不分配/复制。

### 3.2 引用计数（设计稿 A 方案）

| 事件 | 计数 |
|---|---|
| `new` | 1（调用方持有） |
| 局部变量绑定 | 由帧日志接管：`new` 的结果直接 `well_track_owned`；borrowed 引用 `well_track`（retain） |
| 字段赋值 | value +1；旧值 -1 |
| `Model.set` | 对象 +1（已在 Model 中则幂等）；替换同 key 旧对象 -1 |
| `Model.get` / `Model.peek` | +1（返回 owned；帧日志负责 release） |
| `Model.remove` | -1 |
| event 构造字段 | value +1 |
| `emit` | event +1（队列持有） |
| 事件派发完 | event -1 |
| handler/controller 帧结束 | 帧日志中所有引用 -1 |

- **帧日志**：`WellCtx` 持有 `refs[]`；`well_track` retain+记录，`well_track_owned` 只记录（所有权转移给帧），`well_ctx_clear` 统一 release。
- **RC 原子化**：`WellObj.rc` 是 `WellAtomicInt`；Windows 用 `InterlockedIncrement/Decrement`，GCC/Clang 用 `__atomic_*`，其他编译器回退单线程；`retain` relaxed，`release` acq_rel，最后一个 release 负责销毁。`g_live` 仍是单线程调试计数。
  - **POSIX 编译器选择**：TinyCC 0.9.27 的 Linux 端口没有 `__atomic_*`/`__sync_*` 内建函数（会退化成隐式函数声明、链接失败），因此 POSIX 默认优先 gcc/clang；Windows 路径用 Interlocked，TCC 仍是 Windows 默认（见 §7 工具链）。
  - 线程局部状态（panic 目标、源码位置、临时对象栈、HTTP 当前请求）在 POSIX 上使用 `pthread_key_*`（`well_thread.c` 的 `well_tls_*`），不依赖编译器 `__thread`（TCC 不支持）；Windows 仍用 TLS 槽 / `__declspec(thread)`。
- **级联释放**：对象 rc=0 时，生成的 `destroy_wt_<T>` 先 release 所有引用字段，再 `free`。
- **String 释放**：String 从不进入 oid registry、也没有 event payload pin，`well_release` 对 `WELL_T_STRING` 跳过 `well_registry_remove`/unpin（省一次分片自旋锁）。
- **成环**：允许（运行时不做 DFS 检查）；环随 oid 过期整组回收，见 §3.15。
- `WELL_RC_DEBUG=1` 时开启 live 对象计数，进程结束打印 `well: live objects at shutdown = N`（正常应为 0）。计数默认关闭：普通运行不为每次对象分配/释放做全局原子 RMW；C 测试 runner 会自动设置该环境变量，`well_live_count()`/泄漏断言仍可用。

### 3.2.1 DAG / 环检测

> **状态（2026-10-22）**：对象图实现已移除（`well_graph_add_edge/remove_edge` 为 no-op 桩，`WellObj` 不再保留图字段）。成环被允许，由 oid 空闲清理整组回收（见 §3.3「不活跃清理」）。

- 历史设计（未启用）：`graph_edges` 出边表 + 加边前 DFS 判环，命中 panic `"DAG violation"`；因每次引用写入 O(V+E)、且环已由 oid 整组清理兜底，未启用。
- 现在的回收路径：自动路径 `WELL_MODEL_TTL_MS` **默认开启**（默认 60 秒，设为 `0`/负数关闭）→ 对过期未使用 oid 组执行回收；装配期 oid（`well_static_owner_oid()`，当前 0，由启动 ctx 决定）**永不回收**：TTL 扫描直接跳过该 oid（2026-10-23 起内部强制回收入口 `well_force_cleanup_oid` 已删除）。
- 约束：过期回收按 oid 整组释放，判据是 `exec_ref==0` + 调度器空闲；语言层引用由 ctx pin、event payload pin、oid/static 写检查覆盖（见 §3.3），正常 Well 代码不会悬空；C/FFI 裸指针需自行 `well_oid_pin`。

### 3.3 Model / Cache / Store

- **Cache**：64 个 shard，shard = `hash(type_id, oid) & 63`；每个 shard 独立哈希表 + spinlock，key = `(type_id, oid)`；开放寻址 + 线性探测，负载因子 0.7；`get` 在锁内 retain 后返回 owned 引用。
- **Store 接口**：

```c
typedef struct WellStore {
    void *self;
    void *(*select)(void *self, unsigned int type_id, long long oid);
    int   (*upsert)(void *self, void *obj);
    int   (*delete_)(void *self, unsigned int type_id, long long oid);
    void  (*shutdown)(void *self);
} WellStore;
```

- **MemoryStore**：线性表 + spinlock，纯后端；只存编码字节；`select` 从本 Store 字节 decode，ref resolver 只递归本 Store，不再查全局 Cache；`upsert/delete` 只操作字节。
- **Model facade**：
  - `Model.get(Type, oid)`：Cache 命中返回；miss 时 Store.select → Cache 写入 → 返回（read-through）；
  - `Model.peek(Type, oid)`：只查 Cache，不访问 Store；
  - `Model.findByOid(Type, oid)`：`get` 的兼容别名；
  - `Model.set(obj)`：先 Store.upsert，再 Cache.set（write-through）；
  - `Model.remove(Type, oid)`：先 Store.delete，再 Cache.remove；
  - `well_model_use_store` 可替换 backend（后续 MysqlStore）。
- Cache 全局共享，key 为 `(type_id, oid)`，不记录来源；语言不保证一个 key 只属于一个 Model/Store。
- 多 Model 可加载同一 key；`Model.get` 命中全局 Cache 直接返回，miss 才回源；最后一次 `Cache.set` 覆盖缓存。
- `Store.get` 是纯后端读取，不写 Cache；`Store.set` 纯后端写入，不自动更新 Cache；业务写走 `Model.set`。
- 未写回 Store 的缓存修改可能被其他 Model 覆盖；跨 Model/Store 的数据一致性由用户负责。
- **Cache 独立 API**（语言层）：`Cache.get(T, oid)` / `Cache.set(vo)` 只操作 Cache（Model = Cache + Store；`Cache.set` 不写 Store）；`Cache.remove()` 删除当前 `ctx.oid` 的全部 Cache 行（所有类型），Store 不变。
- **手动释放优先**：oid 完成工作后应调用 `Cache.remove()` 释放该 oid 的 Cache 引用；对象仍被 handler/event 持有时会在其释放后销毁。TTL 只做最后保底，不应作为主要释放手段。
- 同一 oid 的旧对象可能仍被 handler/event 持有；Cache 替换不会立即释放旧对象。
- 同一对象在 Model 中只允许一个 key；同 key 不同类型可共存。
- **过期数据回收（TTL 保底，默认开启）**：默认 TTL 60 秒（`WELL_MODEL_TTL_MS` 可覆盖，`0`/负数关闭）；记录 oid 活动时间；清理条件为调度器空闲 + `exec_ref==0` + 超过 TTL；只驱逐 Cache/内存对象，Store 保留；下次 `Model.get` 回源；装配期 oid（当前 0）豁免。
- **回收实现（P0-2，2026-10-22 重写）**：单飞（同一时刻只有一个线程清理）；每轮最多 `WELL_MODEL_TTL_BATCH` 个 oid（默认 64，最大 256），剩余下一轮继续。每个 oid 分 4 步：
  1. `g_sched_mutex`：把队列标记 `cleaning`（隔离），该 oid 不再被调度；
  2. `g_act_lock`：再次校验 `exec_ref==0`/过期，摘除 ActEntry，`well_oid_take_objects` 摘出 registry 对象、`well_cache_discard_oid` 丢弃 Cache 行——这期间 pin/Model.get 被短暂挡住，避免旧对象被拿走；
  3. 释放所有全局锁，`well_force_free_objects` 逐个释放（事件 unpin 正常加锁，不再需要 TLS 重入特判）；
  4. `g_sched_mutex`：清除 `cleaning`，若期间有入队则重新 ready_push + signal。
  - 关键收益：**bulk free 不再持有 `g_act_lock`/`g_sched_mutex`**，其他 oid 的 Model 操作与任务派发不受大组释放影响；全局锁只覆盖"元数据摘除"（registry 分片 / Cache 分片）而非对象数。
- 装配期 oid 在回收入口处直接返回，任何路径都不会回收启动期/static 数据。
- **环与跨 oid 约束**（重要）：
  - 同一 oid 组内的环允许存在，由清理整组回收；
  - 清理**不按 rc 判断**，但 `exec_ref==0` + 调度器空闲保证语言层没有组外引用：handler/Model pin、在飞 event payload（Model 对象）都会阻止清理；普通字段/容器跨 oid 写被拒绝；static 值必须是装配期 oid（String 例外）；
  - 例外：C/FFI 直接持有 `WellObj*` 而未 `well_oid_pin`；event payload 对 event 字段中的全部引用加 pin（2026-10-23 起不再只限 `IN_MODEL` 对象）；
  - 只有 `exec_ref==0` 且调度器无该 oid 运行/排队任务的组才会被清；活跃对象不会被误清；
  - 需要长期保留的数据请保持 oid 活跃（Cache.get/set 刷新时间）或使用 Store 回读。
- Cache 锁是自旋锁（Windows `Interlocked*` / GCC `__atomic_*`）；MemoryStore 是 fake backend，不做事务。
- 业务键 `Model.find` 尚未实现，待 S0 设计。

### 3.4 事件总线

- **事件类型即身份**：订阅项以 `type_id` 为 key；`emit(event, channel)` 直接读 event header 的 `type_id` 派发，不做字符串比较。
- **订阅 channel**：每条订阅带 `channel`（`long`）。`0` = 默认通道；`>0` = 普通通道；`-1 (ALL)` = 通配。
- 派发匹配：`sub.event_type == event.type_id` 且（`sub.channel == ALL` 或 `sub.channel == emit_channel`）。
- `emit` 把 `(event, channel)` 放入该事件 target oid 的队列；`well_run_loop` 按 oid 轮转派发。
- 派发时在订阅锁内快照匹配订阅，锁外调用 handler；`once` 在锁内 claim（置 inactive），保证 at-most-once。
- 订阅表一把 spinlock；订阅节点当前不释放，便于快照安全。
- 每个 handler 的 `WellCtx.oid = event->hdr.oid`，因此写权限校验生效。
- `dispatch_event` 在整个派发期间对 target oid 执行 `well_oid_pin`，所有订阅 handler 执行完并释放 event 后 `well_oid_unpin`；即使 handler 不做 `Model.get/set`，该 oid 的数据在事件执行期间也不会被过期回收。
- `once` 在调用后置 inactive；`off` 置 inactive；节点不立即释放（避免遍历时 UAF）。
- 订阅 owner：每条订阅记录注册时的 controller owner id；`stop(C)` 调用 `well_off_owner(C_id)` 批量置 inactive；`off` 只影响当前 owner 的订阅。
- 订阅 owner（控制器类型/停止时注销）已实现基础版；热更新尚未实现。

### 3.4.1 容器（Array / Map）

- `WellArray`：`WellValue` 数组；`WellMap`：String 键 + `WellValue` 值，插入序；`WellLinked`：双向链表，首尾 O(1)，`get` O(n)。
- `WellValue` 是 union（int/long/bool、double、指针）；引用元素由容器 retain/release，值元素直接存。
- 容器创建时 `oid = ctx.oid`；**写容器前**校验 `container.oid == ctx.oid`（`well_check_oid(..., "container write")`）；插入引用元素时再校验 `element.oid == container.oid`（String 除外），直接自引用 panic。读操作不检查。
- `Array<T>`/`Map<T>` 的 `T` 是引用类型时，容器 `is_ref=1`；值类型时 `is_ref=0`（不误 release 整数）。
- 越界/缺失：返回零值 `WellValue`；值类型得到 0/false/0.0，引用类型得到 `null`。
- `pop()` 把元素的引用所有权转移给调用方（codegen 标记为 owned）；`shift()` 同理（O(n)）。
- Array 方法：`push/pop/get/set/slice/concat/join/indexOf/includes/clear/shift/unshift/sort/reverse`。
  - `sort()` 原地升序插入排序，支持 `int/long/bool/double/String` 元素（其他元素类型报编译错误）；`reverse()` 原地反转。
  - `sort/reverse` 与其它容器写操作一致，**会做 oid 检查**（`object.oid == ctx.oid`）；在 handler 里对启动期创建的数组（装配期 oid，当前 0）排序/反转会 panic `well oid violation: Array.sort/Array.reverse`。
  - `slice(a,b)` / `concat(other)` 返回新数组（引用元素 retain，`is_ref` 沿用源数组）；`join(sep)` 仅支持 `Array<String>`；
  - `indexOf/includes` 按元素类型比较（标量按值、String 按内容、其他引用按指针），比较方式由 codegen 传 kind。
- Map 方法：`get/set/has/delete/keys/values/clear`；`keys()` → `Array<String>`，`values()` → `Array<T>`（均为新数组、插入序）。
- 「take」变体（`well_array_slice_take` 等）：接收者为临时值时先计算再释放接收者，用于 `"a,b".split(",").join("-")` 这类链式表达式。
- `Map` 键为 String，`well_map_set`/`delete` 维护键的 retain/release。
- **哈希表**：String 创建时缓存 FNV-1a 64 位 hash；`WellMap` 使用开放寻址 + 线性探测（负载因子 0.7，容量 2 的幂），`get/set/has` 平均 O(1)。
- **插入序**：`keys/vals` 数组保持插入顺序供 `for-in`/`key_at`；`delete` 需要移位并重建槽位，因此是 O(n)。

### 3.5 oid 与写入检查

- `new Struct()`：`oid = ctx.oid`。controller 的 `ctx.oid = 0`：允许在 `run()` 中 `new` 对象并 `Model.set` 创建**系统级只读数据**（oid=0），handler 可 `Model.get(T, 0)` 读取；运行期字段/容器/Bytes 写会 panic，TTL/过期回收跳过 oid 0。可写业务数据在事件 handler 中创建。
- `new Event(target)`：`oid = target`；省略 target 时用 `ctx.oid`。
- 字段写入：`well_check_oid(target, ctx)`，要求 `target.oid == ctx.oid`。
- 字段引用写入 value：`well_check_value_oid(target, value, ctx)`，要求 `value.oid == target.oid`（String 无 oid，跳过；null 允许）。
- `Model.set/remove`：检查对象/oid 与 ctx.oid 相等。
- event 构造字段：豁免 target/value 的 oid 检查（`well_set_ref` 无 check），符合"event 可携带异 oid 只读引用"。
- event 构造完成后 `well_freeze`：事件字段写入会 panic（"write to frozen event"）；构造期的 `new ... { }` 初始化不受影响。

### 3.6 按 oid 队列 + worker 线程池

- `Map<oid, Queue<Event>>`：每个 oid 一个 FIFO 队列；
- 空闲队列进入 per-worker local deque（双向链表，头尾 O(1)）；worker 先取本地尾部，再偷其他 worker 的队首，最后取全局 injector；
- 同 oid 同时最多一个 runner；异 oid 可并行；
- 队列空即从 map 移除；
- handler 内 `emit` 会入对应 oid 队列；同一 oid 若正在执行则不重复调度；
- **work stealing**：`WELL_WORKERS` 未设置时按可用 CPU 自动取 `max(1, cores*3/4)`（1..64，留约 25% 给 HTTP/WS reader、Timer、OS）；显式 `1..64` 覆盖，`auto` 表示自动；主线程 id=0 也参与；测试 runner 默认强制 `WELL_WORKERS=1` 保持确定性；
- **对象池**（热路径）：事件/任务节点 `WellEventNode` 与 oid 队列 `WellOidQueue` 各有一个上限 256 的空闲链表，入队/出队时复用，避免每个事件一次 malloc/free 与堆锁竞争；
- 平台线程/锁封装在 `well_thread.c`；Windows 用 `CRITICAL_SECTION` + 事件，POSIX 用 pthread；
- `print` 语句通过 runtime 锁串行化。

**panic 隔离与定位**：每个 handler 调用有 `setjmp/longjmp` 边界；`g_panic_env` 是线程局部（Windows TLS 槽 / POSIX pthread TLS key，见 §3.2），handler 内 panic 只终止当前线程的当前任务，释放帧引用，调度器继续处理下一个事件。
- 生成代码在每个语句前调用 `well_source_set(file, line)`；panic 输出格式为 `well panic: <file>:<line>: <msg>`，可定位到 .we 源码行；
- **未捕获 panic（controller/装配期）以 `WELL_PANIC_EXIT_CODE = 70` 退出**（此前为 1）。

---

### 3.7 IdUtil

- `IdUtil.getId(): long`，布局：41 位毫秒时间（epoch 2024-01-01）+ 10 位 serverId + 12 位 sequence；
- `WELL_SERVER_ID` 环境变量配置，范围 0..1023，默认 0；
- 同一毫秒内 sequence 自增；溢出后逻辑时间 +1、sequence 归零（不等待）；
- 时钟回拨：使用 `max(now, last)`，sequence 继续递增，保证进程内单调；
- spinlock 保护 `(lastTimestamp, sequence)`，多 worker 安全；
- 重启/同毫秒/时钟回拨仍可能重复，部署需保证 serverId 唯一并接受该风险。

### 3.8 Struct 元数据

- codegen 为每个 struct/event 生成 `WellFieldDesc[]` + `WellTypeDesc`；
- 字段信息：`name`、`offsetof`、`kind`、ref/container `type_id`、容器 `elem_kind/elem_type_id`；
- `well_register_type_desc` 注册；`well_type_desc` / `well_type_field` 查询；
- `well_visit_fields(obj, fn, user)` 可遍历对象字段；
- `WELL_TYPE_DEBUG=1` 时 shutdown 打印所有类型和字段；
- 元数据是后续 Store/序列化/调试的基础。

### 3.9 行级二进制序列化

- `well_encode(obj)`：`magic "WLS1" + version + type_id + field_count + fields`；
- `well_decode(type_id, oid, buf, len, resolver, user)`：按元数据恢复字段；
- 编码：int/long/bool 为 8 字节、double 8 字节、String 长度+字节、ref 为 `present + type_id + oid`、容器为 `present + count + elements`；
- ref 解码通过 `WellRefResolver` 回调按 `(type_id, oid)` 查找；无 resolver 时为 null；
- 容器元素支持标量/String/ref；不支持嵌套容器内联；
- 解码对象走 `well_new`，容器走 `well_array_new` 等；`well_set_ref` 负责 RC 与 oid 校验；
- 这是行级格式，不内联完整对象图。

### 3.10 JSON 解析

- 语言 API：`Json.parse(Type, String)` 返回 `Type*`（失败 panic）；`Json.tryParse(Type, String)` 失败返回 `null`；`Json.stringify(value[, indent])` 返回 String：参数可为 struct/event/null/标量/顶层 Array/Map/Linked；`indent` 0（默认紧凑）或 1–8 空格 pretty；
- 顶层必须是 object（或 `null`）；未知字段跳过；
- 元数据字段名匹配；缺失字段保留默认值；
- 标量：int/long/bool/double；
- String 支持 `\" \\ \/ \b \f 
 \r \t \uXXXX`，含 UTF-8 与代理对；
- ref：JSON 对象递归解析，oid 与根对象一致；
- Array/Map/Linked：JSON array/object；
- Map key 为 String；
- 解析错误：`parse` panic（handler 内可被隔离），`tryParse` 返回 `null`；
- 解析对象走 `well_set_ref`，保留 RC 与 oid 检查。

### 3.11 File / Host API

- `File.read(path)`：读取整个文件为 `String`；失败 panic；
- `File.write(path, text)`：覆盖写；失败 panic；
- `File.exists(path)`：返回 `bool`；
- `File.remove(path)`：存在则删除；失败 panic；
- `Host.nowMs()`：当前 epoch 毫秒（`long`）；
- `Host.env(name)`：环境变量，不存在返回 `null`；
- `Host.log(...)`：类型化输出到 stderr，和 print 共用串行化锁；
- 文件 API 是同步阻塞调用，适合配置/小文件；大文件后续异步化。

### 3.12 HTTP/1.1 服务器

- API：`Http.route(path, method, Handler)`、`Http.listen(port)`、`Http.respond(reqId,status,body)`、`Http.stop()`；
- `Http.listen` 非阻塞：启动监听线程与超时清扫线程，并 `well_service_hold()`；主线程进入 `well_run_loop`，`Http.stop()` 释放服务后返回；
- listener 只做 accept：每个连接交给独立 reader 线程，连接之间互不阻塞；每连接持有一个 service 计数，连接关闭前 run loop 不会退出；
- reader 线程按需循环读请求（**HTTP/1.1 默认 keep-alive**，`Connection: close` 时单次；HTTP/1.0 需显式 `Connection: keep-alive`），然后等待响应写出（`written`）再读下一个请求；
- **处理器内联执行（默认）**：每个请求有全新 oid，互不冲突，因此 reader 线程直接以 `WellCtx(reqId)` 调用 route handler，省去 task 入队、worker 唤醒和回切；panic 仍由 setjmp 隔离。`WELL_HTTP_INLINE=0` 可回退到“每请求 task 投递到 worker”的旧路径（保留用于对比/特殊场景）；
- **保留跨请求残余字节**（`scratch` 滚动缓冲）：一次 `recv` 多读到的下一个请求/body 尾部不丢弃，下一次循环从缓冲区继续解析，因此支持**顺序 pipelining**（同一连接上流水线发送的请求依次得到响应；同一时刻仍只有一个在飞请求）；
- **pipelined 响应批量发送**：inline handler 的响应先格式化进每连接 `outbuf`；`scratch` 里还有完整下一个请求时继续处理，没有才一次 `send`。`Http.stop()` 在 handler 内调用时先 flush 当前连接，避免 shutdown 抢先。depth32/8 连接实测 953k req/s（v1 88.3k）；
- 空闲连接超过 `WELL_HTTP_IDLE_TIMEOUT_MS`（15s）没有新请求则关闭（`SO_RCVTIMEO`）；发送侧设 `WELL_HTTP_SEND_TIMEOUT_MS`（15s，`SO_SNDTIMEO`），慢读客户端不会永久阻塞连接线程；
- handler 在连接线程（默认）或 worker 线程（`WELL_HTTP_INLINE=0`）执行，run-to-completion；handler 内可 `emit` 业务事件，由另一个 oid 的 handler 稍后响应；
- `Http.respond(reqId, status, body)` 可在任意 oid/handler 中调用，首个响应生效：认领 `responded` 后写响应（`Connection: keep-alive/close` 按请求决定），置 `written` 通知 reader；后续调用为空操作；
- 同步响应快速路径：进入 handler 前把当前请求记录到线程局部（TLS），`Http.respond` 命中同一 reqId 时直接使用该指针，跳过全局请求表锁；异步响应（业务 oid）仍走哈希表；
- 引用计数：请求（list + 连接线程 + task）、连接（list + 每个在飞请求），各自最后引用释放时销毁；响应者（handler/清扫线程）只写响应、不释放请求引用，请求引用统一由连接线程在 `written` 后释放；
- 请求结构体本身有上限 64 的对象池，避免每请求 calloc/free；keep-alive 连接缓存上一次的 path `String`，同路径请求不再分配/哈希；
- 超时清扫线程：`WELL_HTTP_TIMEOUT_MS`（5s）内无人响应则返回 504 并关闭该连接；504 响应强制 `Connection: close`（连接随后即关闭，不给客户端留下"可复用"的错觉），清扫线程写响应期间对请求加引用，避免与连接线程的释放竞争；
- 路由按 method + path 精确匹配，未命中返回 404；连接级缓存上一次的 `method+path -> route`，keep-alive 下命中即跳过模块锁；
- 自写 parser：request line（仅 origin-form `/...`，method 必须是 RFC 7230 token）、headers、严格 Content-Length、body；
- 头部自首字节起总时限 `WELL_HTTP_HEADER_TIMEOUT_MS`（5s）；header 大小上限 `WELL_HTTP_MAX_HEADER`（16KB，命中 `\r\n\r\n` 后仍按头部长度校验），body 上限 1MB；
- 协议校验：HTTP/1.1 缺 `Host` → 400；Content-Length 非法（符号/尾随垃圾/溢出/重复且冲突）→ 400；body 含 NUL → 400（语言层把 body 当 C 字符串，NUL 会截断）；HEAD 由 GET 路由服务、只发响应头（Content-Length 与 GET 相同）；未知路径 404，路径存在但方法不匹配 405；
- 在线连接数硬上限 `WELL_HTTP_MAX_CONNECTIONS`（默认 1024，编译期 `-D` 可调），超出时回 503 并关闭，避免线程/内存耗尽；
- Handler 通过 `HttpRequestEvent` 接收 `reqId/method/path/query/body`；
- codegen 为每个 route 生成 wrapper，构造事件并直接调用 handler；
- handler panic 在 worker 线程用 setjmp 捕获隔离；
- 写响应：`TCP_NODELAY` 关闭 Nagle；响应体 ≤3800 字节且响应头 ≤320 字节时，头+体合并成一次 send（总长 <4096），减少系统调用与分段；
- 当前限制：支持顺序 pipelining（残余字节保留），但每连接同一时刻仍只有一个在飞请求；无 chunked/TLS；每连接一个线程，连接总数受 `WELL_HTTP_MAX_CONNECTIONS`（默认 1024）限制；
- Windows：动态加载 `ws2_32.dll`（TCC 无 winsock 头）；Linux：POSIX socket。

### 3.13 WebSocket

- API：`Ws.route(path, OpenHandler, MessageHandler, CloseHandler)`、`Ws.listen`、`Ws.send`、`Ws.close`、`Ws.stop`；
- `Ws.listen` 非阻塞：启动监听线程并 `well_service_hold()`；主线程进入 `well_run_loop`，`Ws.stop()` 后返回；
- listener 只做 accept，每连接交给独立 reader 线程完成握手与收帧，因此已有连接不会阻塞新连接；
- 每个连接是一个 oid；open/message/close handler 默认在**该连接的 reader 线程**直接执行（同一连接天然串行，省去每帧 task 入队/worker 唤醒），`WELL_WS_INLINE=0` 回退为投递到该 oid 的调度器队列；
- `Ws.send/Ws.close(clientId, ...)` 按 clientId 查连接表，可从任意 handler（含业务 oid）调用；**每连接一把写锁**串行化帧写入（handler 线程与 reader 的 pong/close 不会交错帧），发送时不在全局锁内阻塞 `send`；
- 连接对象引用计数（连接表 + 每个 task）；reader 退出后由最后一个引用释放；
- 每连接持有一个 service 计数：连接未关闭时 run loop 不会退出；
- `Ws.stop` 对连接调用 `shutdown()` 而非 `close()`：reader 线程正阻塞在 recv 上，由它自己关闭 socket，避免 RST 丢掉刚发出的帧；
- **握手**：SHA1 + Base64 `Sec-WebSocket-Accept`；头名大小写不敏感，最后一行头也能解析；握手自 accept 起期限 `WS_HANDSHAKE_TIMEOUT_MS`（10s），握手头块上限 16KB；
- **路由/query**：先按 path（`?` 之前）查路由，路由不存在直接回 HTTP 404（不先回 101）；query 字符串传入 open 事件；
- **超时/上限**：空闲 `WS_IDLE_TIMEOUT_MS`（60s，收到任何帧即刷新）、发送 `WS_SEND_TIMEOUT_MS`（15s）；连接上限 `WS_MAX_CONNECTIONS`（默认 4096，编译期 `-D` 可调，超出回 503）；
- **frame**：text、ping/pong、close；客户端必须 mask；1MB 帧上限（超出回 1009）；不支持分片；二进制帧回 close 1003；RSV/未 mask/非法控制帧等协议错误回 1002，`Ws.close` 主动关闭等待对端回应（带超时兜底）；
- handler panic 由调度器 setjmp 隔离，连接保持；
- v1 限制：无 TLS、无压缩、无同端口 HTTP Upgrade、无背压。

### 3.14 MySQL 客户端与连接池

- 自实现 MySQL wire protocol，无 `.dll`/客户端库依赖；`WellMysqlConn` 句柄 + `WellMysqlPool`；
- 支持 `mysql_native_password`；
- 支持 `COM_QUERY`、OK/ERR、text result set；
- `Mysql.queryJson` 输出数组对象 JSON：字符串做 JSON 转义（控制字符 `\u00XX`），按 lenenc 长度处理，**内嵌 NUL 保留为 `\u0000`**；列数上限 4096；
- 超时：connect 5s（`MYSQL_CONNECT_TIMEOUT_MS`）、读写 15s（`MYSQL_IO_TIMEOUT_MS`），超时报 `MySQL: ... timed out after N ms`；
- `Mysql.exec` 只接受 OK/ERR；若语句返回结果集，先**排空**列定义/行/EOF 到包边界再报错（连接可继续使用）；排空失败则关闭连接；
- 连接池：`Mysql.pool/execPool/queryJsonPool/closePool`；`WellMysqlPool` 提供 acquire/release、max 限制、acquire 超时、release 回池；acquire 前对空闲连接做 `COM_PING`，失效连接丢弃重连；
- v1 仍为同步、无 TLS/prepared statement；
- `caching_sha2_password` 明确拒绝。

### 3.15 oid exec_ref 与 TTL

- RC 保留；
- 每个 oid 维护 exec_ref（handler/event 执行引用计数）；
- `Model.get/peek` 经 ctx pin，handler frame 结束统一 unpin；
- Cache TTL 只由 Cache.get/set 更新；
- oid 过期且 exec_ref==0 时，移除该 oid 的 Cache 行（不再要求 rc==1）；
- oid registry：`well_new` 登记、正常 rc0 注销；oid 过期时按 oid 取出全部对象；
- 过期回收（后台保底，不是用户主动 API）：对象引用置空；String 正常 release；容器 force-clear；对象内存直接释放；
- 运行时不再做环检查；环形数据由 oid 过期 + registry 回收兜底。
- 固定大小对象池：`well_obj_free` 按 size class 入池；`well_new` 优先复用；**新分配按 size class 整块大小（class*16）申请**，避免同一 class 内"小尺寸分配被大对象复用"导致越界写；`well_pool_sweep` 按 `WELL_POOL_TTL_MS` 真正释放；`WELL_POOL_MAX_MB` 限制池大小。
- 局部作用域释放：`well_ctx_mark/release_from` 在 while/for/foreach/block 结束释放本轮局部引用；`well_ctx_replace` 覆盖赋值时原地替换 frame 引用并 release 旧值。

### 3.16 模块注册表

- 核心提供 `WellModule` 注册表；
- HTTP/WS/MySQL 各自暴露 `well_http_module/well_ws_module/well_mysql_module`；
- 生成 main 注册模块并 `well_module_init_all()`；
- `well_shutdown` 通过 `well_module_shutdown_all()` 逆序关闭；
- 核心不再直接调用 HTTP/WS/MySQL shutdown；
- `driver` 按程序使用的 `Http/Ws/Mysql` API 选择模块源文件；生成 main 只注册用到的模块；模块头文件只在使用时 include。

### 3.17 定时器（Timer）

- API：`Timer.once(ms, Handler): long`、`Timer.loop(ms, Handler): long`、`Timer.cancel(id)`；
- 实现在 `core/well_timer.c`：一个 timer 线程（懒启动，`well_shutdown` 时 join），链表保存 timer，约 5ms 轮询到期项（cond 精度限制）；
- 定时器到期后按 timer id 作为 oid 投递一个 task（`well_schedule_task`），handler 因此跑在普通 worker 上并遵守同 oid 串行；
- payload 是 `WellTimerInfo { id, interval, fires }`；codegen 为每个 `Timer.*` 调用生成 `timer_fire_N` wrapper：构造 handler 声明的事件类型，按字段名回填 `id/interval/fires`（字段不存在则跳过），再调用 handler；
- `Timer.loop` 到期后 `deadline = now + interval`（落后时跳过错过的周期，不补发）；`Timer.cancel` 可在任意 handler 内调用；
- 生命周期：有 timer 注册时 timer 线程持有一个 service 计数，全部 timer 结束/取消后释放，`well_run_loop` 才会退出；
- 精度：受 cond 轮询（Windows 10ms）与 timer 线程 5ms 睡眠影响，实际误差约 5–15ms（MVP 可接受）。

## 4. 代码生成要点

生成的 C 标识符统一加前缀/转义，避免 Well 名字与 C 关键字、libc、运行时符号冲突（Well 原名保留在元数据/JSON/诊断中）：

| Well 声明 | 生成的 C 名字 |
|---|---|
| struct/event `X` | 类型 `wt_X`，type id `T_wt_X`，`new_wt_X` / `destroy_wt_X` / `desc_wt_X` |
| handler `H` | `hd_H` + `hd_H_onEvent` |
| controller `C` | `ctl_C` + `ctl_C_ensure` / `ctl_C_run` / `ctl_C_stop` |
| `func f` | `wf_f` |
| namespace `N { f }` / static `S { f }` | `wf_N__f` / `wf_S__f` |
| enum `E` / 成员 | 前缀 `E_` / `E_E_MEMBER`（String 成员生成 `well_string_lit(...)`） |
| const `K` | `C_K`（C 宏） |
| static 字段 `S.f` | `wst_S__f` |
| 局部变量 / 参数 / 字段 | `v_` / `f_` 前缀；C 关键字与保留名自动转义 |

- 每个 struct/event 生成 `typedef struct wt_X wt_X;`、`struct wt_X { WellObj hdr; ... }`、`#define T_wt_X <id>`。
- 生成 `static wt_X* new_wt_X(WellCtx*, long long oid)`：分配 + 应用字段默认值（String 默认 `""`）。
- 生成 `static void destroy_wt_X(void*)`：release 引用字段 + free；`main` 注册到运行时。
- handler 生成 `static void hd_H_onEvent(WellCtx*, void* evp)`，内部 `const E* <用户参数名> = (E*)evp;`（参数名沿用源码）。
- controller 生成 `static void ctl_C_ensure(WellCtx*)` + `static void ctl_C_run(WellCtx*)`；`start(C)` 调 `ctl_C_ensure`，重复启动只执行一次。
- `func` 生成 `static <ret> wf_f(WellCtx* ctx, ...)`，调用时自动补 `ctx`；实参先按源码顺序提升为临时变量，保证求值顺序与编译器无关。
- 每个函数入口 `(void)ctx;` 避免未使用告警。
- 生成 C 保留在 `build/<stem>_<sha1前8>.c`（`<sha1前8>` 是源码绝对路径的 sha1 前 8 位，避免不同目录同名 `main.we` 互相覆盖），便于调试；显式 `-o` 时输出到指定路径（`emit-c -o` 会自动创建父目录）。

---

## 5. 构建与测试

```powershell
cd compiler

# 编译 + 运行示例
python wellc.py run examples\hello.we
python wellc.py build examples\features.we -o build\features.exe

# 只生成 C
python wellc.py emit-c examples\hello.we -o build\hello.c

# 端到端测试（190 个 .we 用例 + 19 个 C 测试；runner 默认 WELL_WORKERS=1）
python tests\run_tests.py

# 一键回归（推荐）：默认全部 .we 用例 + C 测试 + check-cli；
# --quick 只跑 .we 用例 + check-cli；--full 再跑 HTTP bench smoke
python tools\verify.py [--quick|--full]

# 可复现性能基线：http_latency / http_throughput / parser
python tools\bench.py --scenario all --json build\bench\result.json
python tools\bench.py --compare build\bench\baseline.json --threshold 30
# 基线文件与场景说明见 tools/README.md
# POSIX CI：仓库根 .github/workflows/verify.yml，Linux + gcc -O2 跑完整 verify.py（全部 .we 用例 + C 测试 + check-cli）；bench 仅 workflow_dispatch 手动触发
```

构建产物：默认 `build/<stem>_<sha1前8>.c/.exe`（源码绝对路径的 sha1 前 8 位混入文件名，避免不同目录同名源码互相覆盖）；`-o` 显式指定输出；`emit-c -o` 自动创建父目录；`build` 对未解析 `import` 打印 `wellc: warning: ...`（不中断）。

C 编译器查找顺序：`WELL_CC` → 仓库内 `project/tool_ai/tools/tcc/tcc/tcc.exe`（Windows 存在时，默认 TCC）→ PATH 中 `gcc`/`clang`/`tcc`（POSIX 优先 gcc/clang，见 §3.2 的 TinyCC 原子内建函数限制）。使用 gcc：`WELL_CC=gcc`（自动 `-O2 -fwrapv`）。
额外参数用 `WELL_CFLAGS`（如 `-O2 -g`）；检测到 gcc/clang 时默认追加 `-O2`，并**始终**追加 `-fwrapv`（语言溢出语义，`WELL_CFLAGS` 不能关闭）；TCC 使用默认参数（不追加 `-O2`）。切换优化级别后先跑 `tools/verify.py`。
本机验证环境：TinyCC 0.9.27 x86_64（仓库 `project/tool_ai/tools/tcc/`，Windows 默认）；已安装 MinGW-w64 GCC 16.2.0（`D:\Soft\gcc`，x86_64-ucrt-posix-seh，`bin` 已加入用户 PATH；新终端可直接 `gcc --version`）。使用 `WELL_CC=gcc` 构建会自动 `-O2 -fwrapv`。CPU parser 微基准：TCC ≈11–14 µs/parse，GCC -O2 ≈6–8 µs/parse（约 1.8×）；HTTP 网络压测噪声大，未见稳定吞吐差异。gcc -O2 下已跑定向 HTTP/WS/JSON/TTL 用例 + 全部 C 测试，全量 `.we` 用例串行编译过慢（~25s/case），本机未完整跑完。Linux CI（Ubuntu 24.04 + gcc -O2）跑全部 `.we` 用例（无 MySQL 的依赖用例 SKIP）。
测试规模（2026-11-10，V4）：**190 个 `.we` 用例 + 19 个 C 测试**（TinyCC 全量 `verify.py` ≈113 s）；runner 默认 `WELL_WORKERS=1` 保证确定性。需要本机 MySQL 的 `52_mysql`/`53_mysql_pool`/`128_mysql_big_column`/`140_mysql_exec_resultset`/`141_mysql_pool_panic`/`169_mysql_json_escapes` 与 C `mysql_conn_test` 用 `requires.txt` 声明依赖：`127.0.0.1:3306` 不可达时打印 `SKIP`（CI runner 无 MySQL 时不会误报）。
诊断契约：`check` 人类可读模式每行 `FILE:LINE:COL: severity: message`；`--json` 只输出一个 JSON 对象，`diagnostics` 可含多条（同一方法体内仍首错即停），`code` 取值为 `lex`/`parse`/`semantic`/`import`/`internal`；退出码无 error=0（仅 warning 也算 0）、有 error=1、参数/IO 错误=2。

---

## 6. 已知边界 / 下一步

| 缺口 | 说明 |
|---|---|
| 线程池 | 已实现每 oid 队列 + N worker + work stealing；仍无 reduction / 抢占，长 handler 会占一个 worker |
| 容器字面量上下文受限 | `[]`/`{}` 目前只允许 `var` 初始化、赋值、static/struct 字段默认值；作为函数实参/`return` 值/三元分支等会报 checker 错误（先赋给变量） |
| 容器回调方法 | Array/Map/Linked 的常用方法（`push/pop/shift/unshift/slice/concat/join/indexOf/includes/clear/sort/reverse`、Map `keys/values`、下标、`for-of/in`）已实现；`Map.entries` 未实现（需 tuple/泛型），缺 `map/filter/forEach` 等回调方法（需要 lambda） |
| 无泛型/接口/异常 | 仅内置 `Array<T>`/`Map<T>`；用户自定义泛型 v2 |
| 无 `invoke` | D-21 已从语言删除；计算用 `func`，跨 handler 用 `emit` |
| 无 `??` 空值合并 | checker 明确报“未实现”，用三元 `x == null ? y : x` 代替 |
| 环 / DAG 检查 | 运行时不做 DFS/环检查（`well_graph_*` 为 no-op 桩）；环由 oid 过期整组回收兜底 |
| controller oid=0 | 设计语义：controller `run` 中 `new` + `Model.set` 是系统级只读数据（oid=0，可读不可写、不参与 TTL 回收）；需要运行期修改的数据在事件 handler 中创建 |
| String.length 按字节 | 设计稿要求 UTF-8 码点 |
| 热重载 | `stop(C)` 可注销订阅并重新 start；模块卸载/热更新尚未实现 |
| 无持久化/热更新/安全边界 | 设计稿已列为后续 |
| Model 线性表 | 已改为哈希表（`(type_id, oid)`，平均 O(1)）；海量数据仍建议分片 |
| 错误信息 | handler panic 已隔离并继续调度；panic 带 `file:line`；未捕获 panic（controller/装配期）exit(70)；无通用错误码/异常语法 |

---

## 7. 扩展指引

- **加语法**：`ast_nodes.py` → `parser.py` → `checker.py`（类型标注）→ `codegen.py`。
- **加运行时能力**：`runtime/core/wellrt.h` 声明 + 对应模块（`well_model.c` / `well_containers.c` / `well_event.c` / `wellrt.c`）实现 + `codegen.py` 的 builtin 分发。
- **加类型**：`checker.check_type_name`、`codegen.ctype/is_ref`、`new_wt_*`/`destroy_wt_*` 生成。
- **加测试**：`tests/cases/<name>/main.we` + `expected.txt`（或 `expect_fail.txt` / `expect_compile_error.txt` / `rc_debug.txt`）。
