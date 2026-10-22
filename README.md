# Well（well-lang）

Well 是一个 **oid actor 风格**的小型语言与运行时：`.we` 源码由 Python 前端编译为 C99，再由 TinyCC（默认）或 gcc/clang 编译为原生可执行文件。

核心特性：

- oid 数据所有权 + 每 oid FIFO、单 runner、跨 oid 并行
- 事件总线（`on/once/off/emit`）、controller/handler
- 原子引用计数（RC）+ 过期数据回收（TTL 保底）+ 手动 `Cache.remove()`
- Model / Cache / Store（默认 MemoryStore）
- 内置 HTTP/1.1、WebSocket、Timer、MySQL、JSON、File/Host
- 静态类型检查、panic 隔离与源码位置、整数除零检查
- 自带端到端测试、回归脚本和可复现 benchmark

> 项目仍在早期阶段：语法/运行时以 `project/compiler/docs/language_for_ai.md` 为准，Linux/POSIX 仅做了 provisional CI，尚未完整验证。

## 目录结构

```text
.
├── .github/workflows/verify.yml       # GitHub Actions（provisional Linux CI）
├── project/
│   ├── compiler/                      # 编译器、运行时、测试、工具、文档
│   │   ├── wellc/                     # lexer/parser/checker/codegen/driver
│   │   ├── runtime/                   # C99 运行时与 HTTP/WS/MySQL 模块
│   │   ├── tests/                     # .we 端到端用例 + C 测试
│   │   ├── tools/                     # verify.py / bench.py
│   │   ├── docs/                      # 实现说明、给 AI 的语言参考
│   │   ├── examples/                  # 示例程序
│   │   └── build/                     # 构建产物（gitignore）
│   └── document/                      # 路线图、审计、修改日志等设计文档
└── README.md
```

## 快速开始

需要 Python 3 和一个 C 编译器。默认优先使用仓库内 bundled TinyCC（如果存在），也可以显式使用 gcc/clang：

```powershell
cd project/compiler

# 编译并运行
python wellc.py run examples/hello.we

# 编译为 exe
python wellc.py build examples/features.we -o build/features.exe

# 只生成 C
python wellc.py emit-c examples/hello.we -o build/hello.c
```

使用 gcc/clang（driver 默认追加 `-O2`）：

```powershell
$env:WELL_CC = "gcc"
python wellc.py build examples/features.we -o build/features_gcc.exe
```

## 测试与基准

```powershell
cd project/compiler

python tools/verify.py --quick    # 115 个 .we 用例
python tools/verify.py            # .we 用例 + 12 个 C 测试
python tools/verify.py --full     # 再加 HTTP bench smoke

python tools/bench.py --scenario all --json build/bench/result.json
```

`WELL_WORKERS` 未设置时按 CPU 自动适配（`max(1, cores*3/4)`）；测试 runner 固定为 1 以保证确定性。

## 文档

- 语言与运行时参考（给 AI）：[`project/compiler/docs/language_for_ai.md`](project/compiler/docs/language_for_ai.md)
- 实现说明：[`project/compiler/docs/IMPLEMENTATION.md`](project/compiler/docs/IMPLEMENTATION.md)
- 编译器/构建说明：[`project/compiler/README.md`](project/compiler/README.md)
- 路线图与优先级：[`project/document/待开发功能-优先级.md`](project/document/待开发功能-优先级.md)
- 代码审计：[`project/document/核心代码审计.md`](project/document/核心代码审计.md)
- 修改日志：[`project/document/修改日志.md`](project/document/修改日志.md)

## 当前状态

- 115 个 `.we` 端到端用例 + 12 个 C 测试通过；
- 主要验证环境：Windows x64 + TinyCC 0.9.27；另有 MinGW-w64 GCC 16.2.0 的定向/优化构建验证；
- Linux/POSIX 仅有 provisional GitHub Actions 配置，尚未完成完整回归；
- 不支持异常/泛型/接口/元组/闭包等高级特性，详见 `language_for_ai.md` 的 §3.4。
