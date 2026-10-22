# Well compiler tools

## `verify.py` — one-command correctness regression

```bash
python tools/verify.py            # all .we cases + C tests
python tools/verify.py --quick    # .we cases only (fast, no C tests)
python tools/verify.py --full     # all tests + short HTTP bench smoke
```

Checks the toolchain (`WELL_CC` / tcc / gcc / clang via `wellc.driver.find_cc`),
runs the end-to-end cases and (unless `--quick`) the C tests, then prints a
summary and exits non-zero on any failure. `--full` additionally runs
`tools/bench.py --scenario http` with a tiny workload as a build/run smoke test.

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

## POSIX CI

`.github/workflows/verify.yml` runs `verify.py --quick` on Ubuntu with tcc.
It is provisional: the project has been developed on Windows/TCC, so the first
Linux runs are expected to expose platform assumptions (paths, HTTP/WS timing,
`.exe` suffixes) rather than to be green immediately.
