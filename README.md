# Well（well-lang）· V4

Well 是一个 **oid actor 风格**的小型语言与运行时：`.we` 源码由 Python 前端编译为 C99，再由 TinyCC（默认）或 GCC/Clang 编译为原生可执行文件。

**当前主线为 V4**（2026-11-10 从 `WellLangV4` 同步，版本标记 `v4.0`）。V4 = V2 基线 + 《v3 借鉴与 v2 改进建议》的落地实现 + 以性能为首要目标的后续优化；V3 与 V2 是平行版本，V4 只吸收 V3 中经实验证明有效的设计（绑定槽位 journal、源码位置 TLS、MySQL 读缓冲、LTO 显式配置、实验方法），不复制其 region/RC/数组实现。V2/V3 的代码与报告保留在 git 历史与 `project/document/` 中。

核心特性：

- oid 数据所有权 + 每 oid FIFO、单 runner、跨 oid 并行
- 事件总线（`on/once/off/emit`）、controller/handler；V4 的派发使用订阅快照帧内数组 + touch/pin 合并 + 0 订阅者快路径
- 引用生命周期：V4 用**绑定槽位 journal** 持有引用局部变量/参数/foreach 元素/null 声明，配合原子 RC、TTL 保底回收与手动 `Cache.remove()`
- 非逃逸对象帧分配：V4 把零 malloc 的证明扩展到含引用字段、容器字段的 struct（`WELL_FLAG_STACK` + 作用域出口统一释放）
- Model / Cache / Store（默认 MemoryStore）
- 内置 HTTP/1.1、WebSocket、Timer、MySQL、JSON、File/Host；V4 新增 `Host.monoUs()` 与 `well_mono_us()` 高分辨率单调时钟
- 静态类型检查（注解/实参/返回/字段/容器元素/运算符/条件）、诊断带 `FILE:LINE:COL`、panic 隔离与源码位置、整数除零/取模检查
- 自带端到端测试、C 测试、journal 契约、回归脚本，以及交替 A/B 基准运行器与可复现原始数据

## V4 改了什么

| 类别 | 内容 | 实测（V2 → V4，交替 A/B 中位数） |
|---|---|---|
| 正确性（P0） | 引用绑定改为稳定 journal 槽位，修复参数重赋值 / 嵌套别名 / 外层 null / foreach 变异等提前析构 | 14/14 毒化契约通过（基线 2/14） |
| 内存（P2） | 非逃逸 struct 全量帧分配：引用字段、容器字段也放 C 帧，作用域出口统一释放 | `bench_alloc` TCC **-67.9%**、GCC -O2 **-69.4%** |
| 调度 | 条件变量唤醒合并（一次唤醒消费一批任务） | 4 worker **+14.9%** tasks/s |
| MySQL | 8 KiB 连接读缓冲，小包走缓冲、大包直读 | 标量查询 **-15.4%** |
| 事件派发 | 订阅快照帧内数组 + touch/pin 合并 + 0 订阅者快路径 | **+12.3%** events/s |
| 源码位置 | 每线程位置结构 + 单次 TLS 读 | `well_source_set` **6.5 → 1.5 ns** |
| 构建 | `--release-lto`（opt-in，默认关闭） | `bench_alloc` **-2.2%** |
| 测试 | 交替 A/B 运行器、journal 契约、TLS 隔离契约、`Host.monoUs` 高分辨率基准、C 测试 `-UNDEBUG` | 190 `.we` + 19 C + 14 契约全过 |
| HTTP/WS | 5 进程 × 7 轮正式 A/B（TCC 与 `--release` 各一轮） | −1.7% / +0.1%（TCC）、+0.4% / +0.1%（release），均在噪声内 |

### 按 WellTest 跨语言报告的优化（2026-10-26）

按“只保留语法、同 oid 串行、异 oid 并行，其余可放宽”的要求，用
`E:\Work\WellTest\REPORT.md` 的同一套 harness 复测：

| 维度 | 优化前 | 优化后 | 对照 |
|---|---:|---:|---|
| 数组访问 | 0.921 ns | 0.919 ns | C 1.089 |
| 标量算术 | 6.300 ns | **4.253 ns（-32.5%）** | C 4.432 |
| 短生命周期分配 | 47.376 ns | **26.497 ns（-44.1%）** | C 30.069 |
| HTTP keep-alive | 207,731 req/s | 212,843 req/s（单次测量，含环境漂移） | C 229,662 |
| WebSocket 回显 | 206,048 msg/s | 215,483 msg/s（同上） | Java 225,188 |
| MySQL 标量查询 | 24.9 µs | **24.3 µs** | 全场最快 |

> 网络基准的绝对值随机器后台负载漂移（同一份代码同日 192k–213k rps）。第二轮逐请求锁削减
> （IdUtil 块分配、连接内请求复用、路由事件免注册）用**同会话交替 A/B** 测得：
> **HTTP +4.5%**（192,470 → 201,176 rps）、WS +0.2%（噪声内）；随后路由事件帧分配再测得
> HTTP **+0.8%**、WS **+1.8%**（对照上一提交，同会话交替 A/B）。

改动与“放宽点”（单线程阶段普通 RC、检查内联、panic 位置随调用传递）见
[`project/document/v4-性能优化-放宽约束说明.md`](project/document/v4-性能优化-放宽约束说明.md)。
完整实施说明、决策与复现方式见 [`project/document/v4-改进与性能报告.md`](project/document/v4-改进与性能报告.md)。

## 目录结构

```text
.
├── .github/workflows/verify.yml       # GitHub Actions：Linux 全量回归（.we + C 测试 + check-cli）
├── .github/workflows/extension.yml    # GitHub Actions：VSCode 插件编译 + 测试
├── project/
│   ├── compiler/                      # 编译器、运行时、测试、工具、文档
│   │   ├── wellc/                     # lexer/parser/checker/codegen/driver
│   │   ├── runtime/                   # C99 运行时（core）与 HTTP/WS/MySQL 模块
│   │   ├── tests/                     # 190 个 .we 端到端用例、19 个 C 测试、contracts/
│   │   ├── tools/                     # verify.py、bench.py、perf/（ab.py、bench_*）
│   │   ├── docs/                      # IMPLEMENTATION、language_for_ai
│   │   ├── examples/                  # 示例程序
│   │   └── build/                     # 构建产物（gitignore）
│   ├── vscode_extension/              # VSCode 插件（高亮/诊断/格式化/自动导入）
│   ├── document/                      # 语言规范、审计、V4 报告、性能历史（perf/）
│   └── test/lang_bench/               # 跨语言基准程序（C/C++/Go/Java/Erlang/Well）
└── README.md
```

## 快速开始

需要 Python 3 和一个 C 编译器。Windows 下默认使用仓库内 bundled TinyCC（`project/tool_ai/tools/tcc/`，若存在），也可显式使用 GCC：

```powershell
cd project/compiler

python wellc.py run examples\hello.we
python wellc.py build examples\features.we -o build\features.exe
python wellc.py emit-c examples\hello.we -o build\hello.c

# 优化构建（GCC/Clang，自动 -O2 -fwrapv）
python wellc.py build examples\features.we -o build\features_gcc.exe --release

# 显式 LTO（opt-in）
python wellc.py build examples\features.we -o build\features_lto.exe --release-lto
# 或 $env:WELL_RELEASE_LTO = "1"
```

`--release` 会挑选**能真实编译并链接**的优化编译器；Windows 优先 `D:\Soft\gcc\mingw64\bin\gcc.exe`（WinLibs GCC 16.2.0），也可用 `WELL_CC` 指定。

## VSCode 插件

`project/vscode_extension` 提供 Well 语言插件：语法高亮、错误检查（调用 `wellc check --json`）、格式化、自动导入 / 整理导入（排序、去重、移除未使用）、Ctrl+点击转到定义。

```powershell
cd project/vscode_extension
npm install
npm test               # 编译 + 4 组测试（格式化 / 导入解析 / 语法 JSON / 激活冒烟）
npm run package        # 生成 well-language-0.1.0.vsix
code --install-extension well-language-0.1.0.vsix
```

安装后打开任意 `.we` 文件即可；`Ctrl+Shift+P` 中有 `Well: 检查当前文件`、`Well: 格式化当前文件`、`Well: 整理包/导入`、`Well: 自动导入光标处符号`。详细说明见 [`project/vscode_extension/README.md`](project/vscode_extension/README.md)。

## 测试与基准

```powershell
cd project/compiler

$env:WELL_WORKERS = "1"
python tools\verify.py            # 190 .we + 19 C 测试 + check-cli（MySQL 可用时连库用例不跳过）
python tools\verify.py --quick    # 只跑 .we 用例 + check-cli
python tests\contracts\journal_aliases.py   # 14 个 journal 毒化契约

# 可复现性能基线（http_latency / http_throughput / parser）
python tools\bench.py --scenario all --json build\bench\result.json
python tools\bench.py --compare build\bench\baseline.json --threshold 30

# 交替 A/B：每个变体 7 个独立进程、前 2 轮预热、轮换启动顺序
python tools\perf\ab.py `
  --run base=build\perf\alloc_tcc_base.exe `
  --run v4=build\perf\alloc_tcc_new.exe `
  --metric "ns_per_iter\s+([0-9.]+)" --rounds 7 --warmup 2 `
  --json build\perf\ab_alloc_tcc.json
```

`WELL_WORKERS` 未设置时按 CPU 自动取 `max(1, cores*3/4)`；测试 runner 固定 1 worker 保证确定性。原始 A/B JSON 归档在 [`project/document/perf/raw/v4/`](project/document/perf/raw/v4/)。

## 持续集成

- `verify.yml`：push 改动 `project/compiler/**` 时在 Ubuntu + gcc 跑完整 `verify.py`（全部 `.we` 用例 + C 测试 + `check-cli`，无 MySQL 服务时 DB 用例自动跳过）；PR 一律运行；
- `extension.yml`：VSCode 插件编译 + 4 组 `npm test`；
- bench 不进入 push 门禁，通过 `workflow_dispatch` 勾选 `run_bench` 手动触发。

## 文档

- V4 改进与性能报告：[`project/document/v4-改进与性能报告.md`](project/document/v4-改进与性能报告.md)
- V4 放宽约束说明：[`project/document/v4-性能优化-放宽约束说明.md`](project/document/v4-性能优化-放宽约束说明.md)
- 改进意见来源（V2 视角的 V3 借鉴）：[`project/document/v3借鉴与v2改进建议.md`](project/document/v3借鉴与v2改进建议.md)
- 语言规范：[`project/document/语言规范.md`](project/document/语言规范.md)
- 给 AI 的语言参考：[`project/compiler/docs/language_for_ai.md`](project/compiler/docs/language_for_ai.md)
- 实现说明：[`project/compiler/docs/IMPLEMENTATION.md`](project/compiler/docs/IMPLEMENTATION.md)
- 编译器/构建说明：[`project/compiler/README.md`](project/compiler/README.md)
- VSCode 插件：[`project/vscode_extension/README.md`](project/vscode_extension/README.md)
- 路线图与优先级：[`project/document/待开发功能-优先级.md`](project/document/待开发功能-优先级.md)
- 代码审计（第一轮修复版）：[`project/document/核心代码审计.md`](project/document/核心代码审计.md)（110 条全部闭环）
- 代码审计（第二轮复核）：[`project/document/核心代码审计-第二轮.md`](project/document/核心代码审计-第二轮.md)
- 设计计划（内存分配）：[`project/document/per-oid-arena-实施方案.md`](project/document/per-oid-arena-实施方案.md)（实测不具收益，见 V4 报告 §9.4）
- 性能分析（多 worker）：[`project/document/多worker扩展性分析与优化建议.md`](project/document/多worker扩展性分析与优化建议.md)
- 性能历史与原始数据：[`project/document/perf/`](project/document/perf/)
- 修改日志：[`project/document/修改日志.md`](project/document/修改日志.md)

## 当前状态

- 全部 `.we` 端到端用例（当前 **190** 个）+ 全部 C 测试（当前 **19** 个）+ `check-cli` + journal 契约（14/14）在 Windows x64 + TinyCC 0.9.27 下通过（完整 `verify.py` 约 113 s，本机 MySQL 5.7.40 实库，DB 用例未跳过）；
- Linux/POSIX：GitHub Actions（Ubuntu + `gcc -O2 -fwrapv`）跑同一套回归 + `check-cli`；另有 MinGW-w64 GCC 16.2.0 的定向/优化构建验证；
- 编辑器支持：`project/vscode_extension` 提供 VSCode 插件（语法高亮、错误检查、格式化、自动导入/整理包、Ctrl+点击转到定义），配套 `wellc check --json` 诊断接口，插件 CI 独立跑 `npm test`；
- 不支持异常/泛型/接口/元组/闭包等高级特性，详见 `language_for_ai.md` 的 §3.4。
