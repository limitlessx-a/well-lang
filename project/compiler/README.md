# Well 语言 MVP 编译器

`wellc` 把 `.we` 源码编译为 C99，再由 C 编译器生成可执行文件。当前是可工作的 MVP，已打通：

- `module` / `import`（本地相对/点路径解析，声明合并）
- `const` / `enum`（int、String；enum 不再承担事件名；`E.fromInt(v)` / `E.toString(v)`）
- `struct`（单继承字段展平）、`event`（事件类型即身份，无 `bind`）
- 根类型继承可省略：`struct S` ≡ `extends Vo`，`event E` ≡ `extends Event`，`controller C` ≡ `extends Controller`，`handler H` ≡ `extends Handler`
- `controller`（`on/once/off/start/stop/emit/run`）；`start(C)` 单例幂等，`stop(C)` 注销该 controller 类型的订阅并允许重新 start
- `handler`（`onEvent` 事件处理、无字段）
- `func` 函数
- `namespace N { f(a:int):int {...} }` 静态函数集合：`N.f(...)` 编译期解析、生成 `static` C 函数（零运行时开销，不占 oid/RC；成员暂只允许函数）
- `static S { maxLevel: int = 1; title: String = "x"; m(): int {...} }` 静态类（config/工具类）：**启动期（ctx.oid == 装配期 oid，当前 0）可写、运行期只读**的静态字段 + 函数，`S.f` / `S.m()` 编译期解析为 C 全局变量/静态函数；不可实例化；引用字段赋值自动 release 旧值、退出时统一释放；支持字段默认值 `arr: Array<int> = [1,2,3]`、`m: Map<int> = {"k":1}`、`buf: Bytes = new Bytes()`，以及在启动期赋值（`S.arr = [1,2,3]`）；运行期写会 panic（含容器/Bytes 原地变异，靠 oid 检查拦截）；static 数据（装配期 oid，当前 0）永不回收；引用字段类型不能是 event（直接/容器/struct 间接均拒绝，因 emit 会给 oid=0 的 event 分配新 oid）
- **事件路由**：`on(H)` / `on(H, channel)` / `once(H)` / `off(H)`（从 handler 的 `onEvent(e: T)` 参数推断，推荐）；也支持显式 `on(EventType, H[, channel])`，其中 EventType 可以是 handler 参数事件类型本身或其子类（字段展平）；`emit(event)` / `emit(event, channel)`
  - `channel` 是 `long`；`0` = 默认/全局通道；`>0` = 普通通道；`ALL` = 订阅通配
- 基础类型：`int / long / bool / double / String / Bytes / void / null`
  - 整数 `/`、`%` 除数为 0 会 panic（带源码行号）；编译期字面量除零/常量溢出直接报错；运行期溢出明确为二补码 wrap（gcc/clang 默认追加 `-fwrapv`，TCC 实现即 wrap）
  - `String +` 单次拼接正常，但循环 `s = s + x` 是 O(n²)；大量拼接用 `Bytes`（20k 次实测约 0.6s vs Bytes ≈1–2ms）
- 容器：`Array<T>`（`[]`）、`Map<T>`（`{}`，String 键、值同质）、`Linked<T>`（双向链表）
  - `a.push/pop/shift/unshift`、`a.get/set`、`a[i]`、`a.length`、`a.sort()`（int/long/double/String）、`a.reverse()`、`for (x of a)`
  - `a.slice(起,止)`、`a.concat(other)`、`a.join(sep)`（仅 `Array<String>`）、`a.indexOf(v)`、`a.includes(v)`、`a.clear()`
  - `m.get/set`、`m.has/delete`、`m["k"]`、`m.size`、`m.keys()`、`m.values()`、`m.clear()`、`for (k in m)`
  - `l.pushHead/pushTail/popHead/popTail`、`l.head/tail/get`、`l.length`、`for (x of l)`
  - 越界/缺失：值类型返回默认值，引用类型返回 `null`
  - 写容器要求 `container.oid == ctx.oid`，插入引用元素要求 `element.oid == container.oid`
  - `Map` 使用 FNV-1a 64 位字符串 hash + 开放寻址（保留插入序）；`Linked` 首尾 O(1)、get O(n)
- 语句：`var`、赋值、`if/else`、`while`、`for`、`return`、`break/continue`
- 表达式：算术/比较/逻辑、字符串拼接、`String.length`、成员访问、`new`、对象初始化
- **String 字节方法**：`s[i]` / `byteAt(i)` / `charCodeAt(i)`、`substring/slice(a,b)`、`indexOf(x[,from])`、`contains/startsWith/endsWith`、`trim`、`toLower/toUpper`、`split(sep)`、`replace(old, new)`、`String.fromByte(n)`、`String.fromInt(n)`、`s.codePointAt(i)`、`String.fromCodePoint(cp)`（长度与下标均按字节，与 `String.length` 一致；码点方法按 UTF-8 解码）
- **Bytes 可变字节缓冲**：`new Bytes()`、`b.length`、`push/get/set/append/appendStr/clear/toString`；用于增量拼装负载，避免**循环累积** String `+` 的 O(n²)；单次拼接是 O(总长度)；写操作参与 oid 检查（运行期不能改启动期/其它 oid 的 Bytes）
- **Well 版 HTTP 解析器（验证项目）**：`examples/lib/http_parser.we`（纯 Well 解析 request line/headers/body），由 `tests/cases/65_well_http_parser`、`66_http_parser_route` 覆盖，证明协议层可在 Well 内实现
- **性能基线**：`examples/http_parser_bench` 可复现；当前 Well 解析器 ≈13.3 µs/次 vs C ≈0.75 µs/次（≈18×），瓶颈是每字段多次分配，分析见 `document/HTTP-解析器性能基线.md`
- **TCC vs GCC -O2（本机初步）**：CPU parser 微基准 ≈11–14 µs/parse → ≈6–8 µs/parse（约 1.8×）；HTTP 压测受 Python 客户端/OS 抖动影响，未观察到稳定吞吐差异；gcc -O2 编译明显更慢
- oid 写入检查、Model `get/set/remove`（64 分片哈希表 + spinlock）、引用计数（A 方案，原子 RC）、事件队列、订阅表 spinlock + 快照派发、按 oid 队列 + N worker 线程池、线程局部 handler panic 隔离、print 串行化
- **成环策略**：允许成环，运行时不做 DFS 检查；环随 oid 过期由 registry 整组回收（TTL 保底，见 `IMPLEMENTATION §3.15`）
- **Struct 元数据**：每个 struct/event 生成 `WellTypeDesc`/`WellFieldDesc`（字段名、偏移、类型、容器元素类型），运行时可查询/遍历
- **Model facade**：Cache（64 分片）+ `Store` 接口 + 默认 `MemoryStore`；`Model.get` 为 Cache→Store→Cache，`Model.peek` 只查 Cache，`set/remove` 写穿
- **Cache 独立 API**：`Cache.get(T, oid)` / `Cache.set(vo)` 只操作 Cache（不经过 Store）；`Cache.remove()` 删除当前 `ctx.oid` 的全部 Cache 行（所有类型），Store 不变；用于 oid 完成后的手动释放
- **过期 oid 数据回收（TTL 保底）**：**默认开启（60 秒）**，`WELL_MODEL_TTL_MS` 可覆盖、设为 `0` 关闭；oid 空闲且 `exec_ref==0` 时整组回收（含环）+ 丢弃 Cache 行，Store 保留，下次访问自动回源；装配期 oid（当前 0，由启动 ctx 决定）**永不回收**。**oid 完成工作后应主动 `Cache.remove()`；TTL 只是最后保底**
- **行级二进制序列化**：`well_encode` / `well_decode`，支持标量、String、Array/Map/Linked、ref（type_id+oid）；ref 可用 resolver 恢复
- **JSON 解析**：`Json.parse(Type, String)` 按 Struct 元数据解析为 struct/event，支持嵌套对象、Array/Map/Linked、String 转义/Unicode、ref；`Json.tryParse(Type, String)` 失败返回 `null`（不 panic）；`Json.stringify(value[, indent])` 按元数据序列化（struct/event、null、标量、顶层 Array/Map/Linked；嵌套容器；转义；indent 0=紧凑、1–8=pretty）
- **File/Host API**：`File.read/write/exists/remove`、`Host.nowMs/env/log`
- **HTTP/1.1 服务器**：`Http.route/listen/respond/stop`；非阻塞 listener + 每连接 reader 线程（**keep-alive**，HTTP/1.1 默认开，15s 空闲关闭）+ 每请求 task 派发（handler 在 worker 上运行，可 emit 业务事件后由其他 oid 响应）+ 首响应生效 + 5s 超时 504；自写受限 parser；Windows Winsock 动态加载，Linux POSIX socket
- **WebSocket**：`Ws.route/listen/send/close/stop`；非阻塞 listener + 每连接 reader 线程，open/message/close handler 作为 task 在连接 oid 上串行执行；`Ws.send/close` 可从任意 handler 按 clientId 调用；握手、text frame、ping/pong、close；独立端口
- **Timer**：`Timer.once(ms, Handler): long`、`Timer.loop(ms, Handler): long`、`Timer.cancel(id)`；到期以 timer id 为 oid 投递 task，handler 在 worker 上执行；有 timer 时进程保持运行
- **MySQL 客户端（最小）**：自实现 wire protocol；`mysql_native_password` + `COM_QUERY`；`Mysql.connect/exec/queryJson/close`
- **MySQL 连接池**：`Mysql.pool/execPool/queryJsonPool/closePool`；`WellMysqlPool` acquire/release
- **oid exec_ref**：handler/event 执行引用租约（event 派发期间对 target oid 额外 pin）；Cache TTL 由 Cache.get/set 更新；oid 过期后回收 Cache 行
- **oid registry + 过期回收**：按 oid 枚举全部对象；exec_ref==0 时整图回收（含环）
- **固定大小对象池**：struct/event 回收后复用；`WELL_POOL_TTL_MS` 到期真正释放；`WELL_POOL_MAX_MB` 上限
- **局部作用域释放**：loop body / block / function / return / break / continue 结束释放局部引用；配合 `well_ctx_replace` 覆盖赋值释放

- `IdUtil.getId(): long`：`serverId + timestamp + sequence`，线程安全，`WELL_SERVER_ID` 配置 0..1023

## 目录

```
compiler/
  wellc.py          # CLI 入口
  wellc/            # lexer / parser / checker / codegen / driver
  runtime/          # wellrt / well_model / well_containers / well_event（C99）
  examples/         # 示例 .we
  tests/            # 端到端测试（115 个 case + C serialize/store/cleanup/cleanup_batch/mysql/force/ttl/pool/reload/race/pure/work-stealing tests）
  docs/             # 实现说明
  build/            # 生成的 C / 可执行文件（构建产物）
```

## 使用

需要 Python 3 和一个 C 编译器。本机已验证 TinyCC 0.9.27（仓库内 `tool_ai/tools/tcc/`）与 MinGW-w64 GCC 16.2.0（`D:\Soft\gcc`，已加入用户 PATH；`WELL_CC=gcc` 自动 `-O2 -fwrapv`）。

```powershell
cd compiler

python wellc.py run examples\hello.we
python wellc.py build examples\features.we -o build\features.exe
python wellc.py emit-c examples\hello.we -o build\hello.c

# 只做词法/语法/语义检查：不生成可执行文件，不写 build/
python wellc.py check examples\hello.we
python wellc.py check examples\hello.we --json
```

`check` 复用 `load_program` + `Checker`，同时检查 import 是否可以解析（解析失败给 warning）。人类可读模式每行 `FILE:LINE:COL: severity: message`；`--json` 只输出一个 JSON 对象：

```json
{"ok": false, "file": "...", "diagnostics": [
  {"file": "...", "line": 3, "col": 7, "endLine": 3, "endCol": 24,
   "severity": "error", "message": "...", "code": "semantic"}
]}
```

退出码：无 error 为 0（仅 warning 也算 0），有 error 为 1，参数/IO 错误为 2。`project/vscode_extension` 插件的错误检查即调用 `wellc.py check --json`；`tools/verify.py` 也带一个 `check-cli` 自测（覆盖无 entry 的库文件、编译错误用例、未解析 import）。

运行测试 / 一键回归：

```powershell
python tools\verify.py            # 全量：115 个 .we 用例 + C 测试（含 check-cli 自测）
python tools\verify.py --quick    # 只跑 .we 用例 + check-cli 自测
python tools\verify.py --full     # 全量 + HTTP bench smoke
```

性能基线（可复现，非临时脚本）：

```powershell
python tools\bench.py --scenario all --json build\bench\result.json
python tools\bench.py --scenario http --save-baseline build\bench\baseline.json
python tools\bench.py --compare build\bench\baseline.json --threshold 30
```

编译器查找顺序：环境变量 `WELL_CC` → 仓库内 `tool_ai/tools/tcc/tcc/tcc.exe`（Windows 存在时，默认快速 TCC）→ PATH 中的 `gcc`/`clang`/`tcc`（POSIX 优先 gcc/clang：TinyCC 0.9.27 的 Linux 端口没有运行时所需的 `__atomic_*`/`__sync_*` 内建函数，Windows 路径改用 Interlocked，因此 TCC 仍是 Windows 默认）。

额外 C 编译参数用 `WELL_CFLAGS` 传入（如 `-O2 -g`）；当检测到 gcc/clang 时默认追加 `-O2`，并**始终**追加 `-fwrapv`（语言溢出语义，`WELL_CFLAGS` 不能关闭）；TCC 使用默认参数（不追加 `-O2`）。

### C 工具链（本机）

| 工具链 | 位置 / 版本 | 用途 |
|---|---|---|
| TinyCC | 仓库内 `tool_ai/tools/tcc/`，0.9.27 | **默认**，编译快，driver 不追加 `-O2`，适合日常构建/测试 |
| MinGW-w64 GCC | `D:\Soft\gcc`，16.2.0（x86_64-ucrt-posix-seh） | 性能验证/优化构建；`bin` 已在用户 PATH |

```powershell
# 新开终端后可直接使用 gcc
gcc --version

# 用 gcc 构建（driver 自动追加 -O2 -fwrapv）
$env:WELL_CC = "gcc"
python wellc.py build examples\features.we -o build\features_gcc.exe

# 自定义 flags，或显式关闭自动 -O2（-fwrapv 仍会保留）
$env:WELL_CFLAGS = "-O2 -g"
$env:WELL_CFLAGS = ""
```

验证状态（2026-10-22）：GCC 16.2 -O2 -fwrapv 下定向 HTTP/WS/JSON/TTL 用例与全部 12 个 C 测试通过；CPU parser 微基准约 6–8 µs（TCC 约 11–14 µs，≈1.8×）；HTTP 压测噪声大，暂未观察到稳定吞吐差异；gcc 全量 115 用例串行编译约 25s/case，未完整跑完。Linux CI（Ubuntu 24.04 + gcc -O2）的 115 个 `.we` 用例已跑通（2 个 MySQL 用例跳过）。

### CI（Linux）

`.github/workflows/verify.yml` 在 Ubuntu 上用 gcc 跑完整 `tools/verify.py`（**115 个 `.we` 用例 + 12 个 C 测试 + check-cli**；POSIX 上 `find_cc` 优先 gcc/clang，自动 `-O2 -fwrapv`）。2026-10-23 起 **CI 已转正**：C 测试纳入每次 push/PR 的 Linux 门禁；`52_mysql`/`53_mysql_pool` 因 runner 无 MySQL 自动 SKIP。bench（HTTP 压测/解析器基线）不进入 push 门禁，通过 `workflow_dispatch` 勾选 `run_bench=true` 手动触发，避免共享 runner 的计时噪声。需要本机 MySQL 的用例通过用例目录里的 `requires.txt` 声明依赖：`127.0.0.1:3306` 不可达时打印 `SKIP` 而不是误报失败。

插件 CI 在 `.github/workflows/extension.yml`：`project/vscode_extension/**` 变化时跑 `npm test`（TypeScript 编译 + 格式化/import/语法/激活冒烟 4 组测试）。


调试引用计数：

```powershell
$env:WELL_RC_DEBUG = "1"
.\build\features.exe   # stderr: well: live objects at shutdown = 0
```

## 说明（MVP 边界）

- 线程池 + 每 oid 队列：`WELL_WORKERS` 未设置时按 CPU 自动取 `max(1, cores*3/4)`（8 核 → 6），可显式设 1..64 或 `auto`；per-worker local deque + work stealing；同 oid 严格 FIFO，异 oid 可并行；测试 runner 默认 `WELL_WORKERS=1` 保证确定性。
- handler panic 会隔离并继续调度；panic 输出带 `file:line`；controller/装配期未捕获 panic 以退出码 **70** 退出。
- `print` 语句通过 runtime 锁串行化，避免多线程输出交错。
- 没有实现 Array/Map 的完整 JS 方法（`shift/unshift/slice/concat/join/indexOf/includes`、Map `keys/values/entries`），也没有 `map/filter/forEach`（需要 lambda）。
- 没有实现泛型（除内置 Array/Map）、接口、异常、通用 `for-in`（只支持 Map 键迭代）、持久化、热更新；`switch/case` 与 enum `fromInt/toString` 已支持；`invoke` 已从语言中删除（D-21），不是遗漏。
- 允许成环：不立即释放，随 oid 过期整组回收；长寿命/热点 oid 的环需要显式回收或子 oid 拆分。
- 字段引用写入同时校验 `target.oid == ctx.oid` 与 `value.oid == target.oid`；event 构造豁免。
- controller 的 `ctx.oid = 0`：在 `run()` 中 `new` + `Model.set` 创建的是**系统级只读数据**（oid=0，可读不可写、不参与 TTL 回收）；需要运行期修改的业务数据在 handler（目标 oid）中创建。
- `String.length` 按 UTF-8 字节数（设计稿要求码点）。
- 生成 C 使用 C99（兼容 TinyCC）；线程/调度已实现（Windows CreateThread / POSIX pthread）。
- HTTP/1.1 仍是受限实现：支持 keep-alive（每连接一个 reader 线程、15s 空闲超时），但不支持 pipelining 并发、chunked、TLS/HTTP2；`Http.respond` 可在任意 handler 中首个生效，5s 未响应返回 504 并关闭连接。
- Model 已拆为 Cache + Store（默认 MemoryStore，字节行）；MySQL/FileStore 尚未接入。

详细实现、运行时设计、扩展指引见 [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)。
