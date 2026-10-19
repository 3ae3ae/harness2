# harness2-benchmarks

The three benchmarks of the paper
[Harness²: Recursive Agent Harnessing for an Open World](https://arxiv.org/abs/2610.14224),
packaged as suites for the `harness2` command of [Harness² CLI](../../README.md):

| Suite | Benchmark | Work |
|---|---|---|
| `jb` | [JobBench](https://github.com/Job-Bench/job-bench-eval) | professional deliverables across occupations |
| `lab` | [LAB](https://github.com/harveyai/harvey-labs) | legal work products |
| `wb-office`, `wb-web`, `wb-code` | [WorkBuddy-Bench](https://github.com/Tencent/workbuddy-bench) | office documents, web apps, code |

They run on the OpenCode or Codex executor, use the pinned upstream revisions and
compatibility patches of the paper, and grade with each benchmark's own judge.

## Install

From a checkout of this repository, next to `harness2-cli`:

```bash
uv pip install -e . -e 'plugins/harness2-benchmarks[render]'
harness2 setup lab            # or jb, wb-office, wb-web, wb-code
```

Setup downloads the upstream checkouts, task data and a pinned OpenCode build into
`~/.cache/harness2/benchmarks/` (set `HARNESS2_BENCH_DEPS` to put them elsewhere).
[Setup](docs/SETUP.md) covers system requirements, model credentials and the Codex
executor.

## Run

The benchmarks keep the paper's configuration: models and endpoints come from
`HARNESS2_*` environment variables (or `.harness2/.env`), as described in
[Setup](docs/SETUP.md#models-and-credentials). Choose the executor with
`-c executor.name=opencode` or `-c executor.name=codex`:

```bash
harness2 -c executor.name=opencode -c suite.name=lab doctor
harness2 -c executor.name=opencode run --suite lab --k 1 --mode parallel \
  --only-domains antitrust-competition --max-tasks 1
harness2 judge
harness2 report
```

[Benchmarks](docs/BENCHMARKS.md) describes each benchmark's data, domains and grading;
[Experiments](docs/EXPERIMENTS.md) covers task selection, concurrency, reproducibility and
network restrictions during evaluation.

## Licenses

The vendored runners and patches under `harness2_benchmarks/third_party/` keep their
upstream licenses; see [third-party notices](harness2_benchmarks/third_party/README.md).
Benchmark data is downloaded from its publishers under their terms.
