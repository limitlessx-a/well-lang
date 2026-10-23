# Well compiler tools

## `verify.py` — one-command correctness regression

```bash
python tools/verify.py            # all .we cases + all C tests + check-cli
python tools/verify.py --quick    # .we cases + check-cli (fast, no C tests)
python tools/verify.py --full     # all tests + short HTTP bench smoke
```

Checks the toolchain (`WELL_CC` / tcc / gcc / clang via `wellc.driver.find_cc`),
runs the end-to-end cases, the `check-cli` self-test and (unless `--quick`) the
C tests, then prints a summary and exits non-zero on any failure. `--full`
additionally runs `tools/bench.py --scenario http` with a tiny workload as a
build/run smoke test; `bench.py` exits non-zero when the HTTP run reports
client errors, so the smoke also asserts `errors == 0`. The test runner forces
`WELL_WORKERS=1` for deterministic ordering.

Cases that need a local MySQL (`52_mysql`, `53_mysql_pool`,
`128_mysql_big_column`, `140_mysql_exec_resultset`, `141_mysql_pool_panic`,
`169_mysql_json_escapes`) and the C `mysql_conn_test` declare the dependency in
`requires.txt` and print `SKIP` when `127.0.0.1:3306` is unreachable.

## `bench.py` — reproducible benchmarks

```bash
python tools/bench.py --scenario all --json build/bench/result.json
python tools/bench.py --scenario http --save-baseline build/bench/baseline.json
python tools/bench.py --compare build/bench/baseline.json --threshold 30
```

Scenarios:

| scenario | metric |
|---|---|
| `http_latency` | sequential keep-alive `GET /bench`: avg / p50 / p99 µs |
| `http_throughput` | N keep-alive clients for S seconds: req/s |
| `parser` | `lex` + `parse_program` of `bench_http_server.we` (compiler frontend): avg / p50 / p99 µs |

Notes:

- `bench_http_server.we` is a template containing `{{PORT}}`; `bench.py` fills in
  a free port and builds it under `build/bench/` on every run.
- `--compare` checks **p50 latency**, **req/s** and **parser avg** against a
  baseline with `--threshold` percent slack (default 30, higher is better for
  req/s). Exit code is non-zero on regression.
- Baselines are environment specific; regenerate with `--save-baseline` after
  intentional performance changes and commit the JSON if desired.
- Build artifacts stay in `build/bench/` (gitignored).
- A run whose HTTP throughput scenario reports client errors (`errors > 0`)
  exits non-zero; `verify.py --full` therefore fails its bench smoke instead of
  accepting a run with failed requests.
- Worker count defaults to `max(1, cores*3/4)`; set `WELL_WORKERS` explicitly
  when comparing baselines across machines/settings.
- Compiler comparison: set `WELL_CC=gcc` (driver adds `-O2` by default) or
  `WELL_CFLAGS="-O2 -g"` before running `bench.py`; TCC remains the default and
  uses its default flags (the driver does not add -O2). Keep the same machine/load when comparing baselines.
- First local comparison (Windows x64): `examples/http_parser_bench` CPU parse
  ≈11–14 us (TCC) vs ≈6–8 us (GCC 16.2 -O2), ~1.8x; HTTP network benchmarks are
  noisy with the Python client and showed no stable throughput difference.

## POSIX CI

The repository-root `.github/workflows/verify.yml` runs the **full**
`tools/verify.py` on Ubuntu with gcc: all `.we` cases + all C tests + the
`check-cli` self-test, on every push/PR that touches `project/compiler/**`.
POSIX builds use gcc because TinyCC 0.9.27's Linux port lacks the `__atomic_*`
builtins the runtime needs; the driver adds `-O2 -fwrapv` there automatically.
Cases that need a local MySQL (`52_mysql`, `53_mysql_pool`,
`128_mysql_big_column`, `140_mysql_exec_resultset`, `141_mysql_pool_panic`,
`169_mysql_json_escapes`) and the C `mysql_conn_test` print `SKIP` when
`127.0.0.1:3306` is unreachable. Benchmarks are **not** part of the push gate:
the `bench` job only runs for manual `workflow_dispatch` with `run_bench=true`,
to avoid timing noise on shared runners.
