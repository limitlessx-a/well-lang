# Well 性能基准目录

本目录保存 v1 → v2 性能优化的基准数据、方法与环境说明。所有数字都是**同机相对比较**，
不要当成跨机器绝对值。

## 文件

| 文件 | 内容 |
|---|---|
| `v1-vs-v2-性能对比-2026-10-23.md` | 第一批优化（字面量哈希、HTTP/WS 内联、响应快速路径、48B 对象头）的完整对比与结论 |
| `summary-2026-10-24.md` | 阶段汇总：主场景 A/B、原语、长稳、正确性与未完成项 |
| `scheduler-per-worker-设计.md` | 下一阶段 per-worker 队列/锁调度器的设计、阶段与验收门槛 |
| `raw/*.json` | 压测驱动直接输出的原始 JSON（每轮 p50/p99/rps/RSS） |
| `verify-final-inline.log` | inline 默认开启时的全量 `.we` 回归（189/189） |
| `verify-c-tests-tccrun.log` | 18 个 C 测试（tcc-run 模式） |
| `verify-inline-default.log` | 早期 inline 默认开启验证记录 |

## 工具

均在 `project/compiler/tools/perf/`：

- `bench_http.py`：HTTP keep-alive 吞吐/延迟（多进程客户端，可指定 .we 服务器与路径）
- `bench_http_pipeline.py`：HTTP/1.1 pipelining 吞吐（`--depth` 1/8/16/32）
- `test_http_mixed.py`：pipelined route + 404 响应顺序回归检查
- `bench_ws.py`：WebSocket 回显吞吐/延迟（最小 RFC 6455 客户端）
- `bench_runtime.c`：运行时原语微基准（对象/String/IdUtil/Array）
- `bench_literals.we` / `bench_alloc.we`：字面量查找、对象生命周期微基准
- `bench_json.we` / `bench_json_long.we`：JSON parse/stringify 微基准（150B 对象 / 1KB 字符串字段）
- `bench_sched.c`：调度器 task 吞吐（emit/定时器路径，W=1/4）
- `bench_sched_multi.c`：多生产者调度器吞吐（4 producer，W=1/2/4）
- `bench_soak.py`：持续压测下的 RSS 斜率/长稳 smoke（`--progress file.jsonl` 实时落样本）
- `bench_churn.c`：1MB String 分配/复制/释放 churn（默认 60s，约 4.5GB/s）
- `compare.py`：同一负载 A/B 对比两个 checkout（v1 vs v2），带回归阈值
- `ws_echo_server.we` / `mysql_server.we`：压测用 Well 服务器

## 本机限制：Device Guard / Smart App Control

本机 Smart App Control 启用后会拦截**新链接的未签名 exe**（`WinError 4551`，
“blocked by your organization's Device Guard policy”）。因此基准/回归使用
`tcc ... -run`：TinyCC 在内存中编译并执行生成的 C，进程仍是受信任的 `tcc.exe`。
这只是执行方式，不改变代码或 TCC 的代码生成；CI/普通开发机仍用原生 exe。

`tests/run_tests.py` 提供同一机制：

```powershell
$env:WELL_TEST_EXEC = "tcc-run"
python tools/verify.py
```

## 复现

```powershell
cd project/compiler

# HTTP（v2）
python tools/perf/bench_http.py --label v2 --workers 1 --clients 8 --seconds 2 --repeat 3

# 同一负载跑 v1
$env:BENCH_COMPILER = "E:\Work\WellLang\project\compiler"
python tools/perf/bench_http.py --label v1 --workers 1 --clients 8 --seconds 2 --repeat 3
```

注意：

- 压测客户端是 Python 多进程；8 连接在 6C/12T 机器上约能压到 160–200k req/s，
  更高吞吐需要 C 客户端或更多机器。
- 每轮都会重新编译服务器（`tcc -run`），编译时间不计入吞吐。
- p50/p99 取单连接顺序请求；吞吐取多连接聚合。
- 运行前确认 3306 端口的 MySQL 可用（`mysql_server.we` 用 root/db@root/well_bench）。

## 本地 MySQL 基准实例（独立数据目录）

为了不碰用户已有数据，基准使用独立 data 目录 `E:\Work\mysql-bench\data`，
root 密码 `db@root`，端口 3306，只绑定 127.0.0.1：

```powershell
$m = 'D:\Soft\phpEnv\server\mysql\mysql-5.7'
$d = 'E:\Work\mysql-bench'

# 首次初始化（只需一次）
& "$m\bin\mysqld.exe" --initialize-insecure --basedir="$m" --datadir="$d\data" --console

# 启动（后台）
Start-Process "$m\bin\mysqld.exe" -ArgumentList @(
  "--basedir=$m", "--datadir=$d\data", "--port=3306",
  "--bind-address=127.0.0.1", "--skip-ssl", "--console"
)

# 首次启动后设置密码/建库
& "$m\bin\mysql.exe" --host=127.0.0.1 --port=3306 --user=root -e `
  "ALTER USER 'root'@'localhost' IDENTIFIED BY 'db@root'; CREATE DATABASE IF NOT EXISTS well_bench;"

# 停止
Get-Process mysqld | Stop-Process -Force
```

`tests/cases/52_mysql` 等用例声明 `requires.txt`，3306 不可达时会自动 SKIP。

