# Setup guide

Start with the [quick start](../../../README.md#install). This guide covers benchmark
dependencies, model credentials, and the optional Codex executor. For individual
benchmarks, see the [benchmark guides](BENCHMARKS.md).

## Requirements

Use Linux and Python 3.12 for benchmark runs; the Python package also supports
3.13. On Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y git curl unzip ca-certificates pandoc poppler-utils
```

Pandoc and Poppler are needed by LAB. Also install:

- [Node.js](https://nodejs.org/en/download) 20 or newer.
- [uv](https://docs.astral.sh/uv/getting-started/installation/).
- [Google Cloud CLI](https://cloud.google.com/sdk/docs/install).
- [Docker Engine](https://docs.docker.com/engine/install/) for WorkBuddy-Bench,
  with permission to run `docker info`.

Benchmark setup installs its own pinned Bun runtime under `~/.cache/harness2/benchmarks/`.
Keep the main Harness² environment active when running the commands below.

## Models and credentials

Put your Vertex AI project and model IDs in `.harness2/.env` (or export them in the
shell), then authenticate:

```bash
# .harness2/.env
GOOGLE_CLOUD_PROJECT=my-gcp-project
HARNESS2_SOLVER_MODEL=google-vertex/gemini-3.5-flash
HARNESS2_IMPROVER_MODEL=google-vertex/gemini-3.5-flash
HARNESS2_JUDGE_MODEL=gemini-3.5-flash
```

```bash
gcloud auth application-default login
```

Shell environment variables take precedence over `.harness2/.env`.
`GOOGLE_CLOUD_PROJECT` identifies your project; `GOOGLE_CLOUD_LOCATION` defaults
to `global`. Your account must have access to each configured model.

| Setting | Meaning |
|---|---|
| `HARNESS2_SOLVER_MODEL` | Executor model in OpenCode's `provider/model` format |
| `HARNESS2_IMPROVER_MODEL` | Improver model in the same format; defaults to the executor |
| `HARNESS2_JUDGE_MODEL` | Bare judge model ID; the adapter supplies the Vertex prefix |
| `HARNESS2_PATHS__RUNS` | Output root; defaults to `.harness2/runs/` |

The provided configuration uses Vertex Gemini executors and judges, and supports
either Vertex Claude or Gemini as the improver. LAB also supports the upstream
judge's direct providers when their credentials are configured. For WorkBuddy,
setup generates the Gemini solver and judge routes from these settings; rerun
setup after changing either model.

### Authentication alternatives

On a machine with a configured attached service account, Application Default
Credentials can use that account. Other options include:

- `gcloud auth application-default login --no-browser`, which prints a command
  to run on another machine that has a browser.
- `GOOGLE_APPLICATION_CREDENTIALS` pointing at a service account key file.
- `gcloud auth application-default login --impersonate-service-account=<account>`.

Keep `gcloud` on `PATH` for these Vertex configurations, including the key-file
route: the installation check runs
`gcloud auth application-default print-access-token`.

## Benchmark installation

Install only the benchmark you intend to run:

```bash
harness2 setup lab
harness2 -c executor.name=opencode -c suite.name=lab doctor
```

Use `jb` for JobBench or `wb-office` for WorkBuddy-Bench. See the
[benchmark guides](BENCHMARKS.md) for subset selection and licensing.

The runner overlays and compatibility patches are included in
[`third_party/`](../harness2_benchmarks/third_party/README.md). Setup clones supported upstream
revisions under `~/.cache/harness2/benchmarks/`, applies the patches, creates the benchmark
environments, and downloads task data.

Each benchmark environment lives inside its upstream checkout:

- `~/.cache/harness2/benchmarks/harvey-labs/.venv`
- `~/.cache/harness2/benchmarks/job-bench-eval/.venv`
- `~/.cache/harness2/benchmarks/workbuddy-bench/.venv`

Harness² selects these interpreters automatically; do not activate them for the
main commands.

### Installation checks

`harness2 setup <suite> --dry-run` previews setup without making changes.
`harness2 doctor` checks installed dependencies, credentials, and configuration
without calling a model. Refinement runs perform the same checks, including
judge configuration, before starting.

These checks do not test model availability. Access to an executor, improver, or
judge model is tested when that model is first called. Refinement and judging
return a nonzero exit status when incomplete.

## Codex executor

The Codex executor connects to a Responses API endpoint. For Gemini on Vertex
AI, `harness2 bridge` provides this interface through a local LiteLLM server, using
the same Application Default Credentials.

Add a Vertex provider to `.harness2/config.toml`, install the optional dependencies and
start the bridge in a separate session:

```toml
[providers.vertex]
type = "vertex"
project = "my-gcp-project"
region = "global"
```

```bash
uv pip install -e '.[bridge]' -e 'plugins/harness2-benchmarks[render]'
harness2 bridge --model vertex/gemini-3.5-flash   # for WorkBuddy: add --host 0.0.0.0
```

The bridge prints its endpoint and access key. Set `HARNESS2_CODEX_BASE_URL` to that
endpoint (`http://127.0.0.1:<port>/v1`), `HARNESS2_CODEX_API_KEY` to the key and
`HARNESS2_CODEX_MODEL` to the model name in `.harness2/.env`. The port and key are stored
under `~/.cache/harness2/bridge/` and reused on restart. Pass `-c executor.name=codex` to setup, checks, refinement, judging, and
reporting:

```bash
harness2 setup lab --substrate cx
harness2 -c executor.name=codex -c suite.name=lab doctor
harness2 -c executor.name=codex run --suite lab --k 1 --mode parallel \
  --only-domains antitrust-competition --max-tasks 1
```

WorkBuddy executes Codex inside Docker containers, so the bridge must listen on
`--host 0.0.0.0`, and `HARNESS2_CODEX_CONTAINER_URL` must give the address the containers
reach it at, usually `http://host.docker.internal:<port>/v1`. This binds the bridge to all
network interfaces; restrict access to the intended clients.

To use another Responses API endpoint, set `HARNESS2_CODEX_BASE_URL`,
`HARNESS2_CODEX_API_KEY`, and `HARNESS2_CODEX_MODEL` directly. Setup records the
endpoint and model in `~/.cache/harness2/benchmarks/codex/home/config.toml`; rerun setup after
changing either. `harness2 doctor` reports stale configuration until then.

Codex runs with its sandbox disabled. Review the
[executor network restrictions](EXPERIMENTS.md#web-access) before evaluation.

## Network access during setup

Setup downloads dependencies from the following hosts:

| Host | Used for |
|---|---|
| `github.com` | Pinned upstream checkouts |
| `registry.npmjs.org` | Pinned Bun runtime and Codex CLI, plus dependencies resolved by `bun install` |
| `huggingface.co` and its CDN | JobBench data and WorkBuddy subset archives |
| A Docker registry | WorkBuddy container image |
| `astral.sh` | CPython 3.12 when the host has no system Python 3.12 |

Use `harness2 setup <suite> --dry-run` to review setup commands when configuring
network access. Experiments also access the model endpoints configured in
`.harness2/.env`. Setup downloads and executor web access have different requirements;
see [web access during evaluation](EXPERIMENTS.md#web-access).

## Wheel installations

A wheel ships the `harness2_benchmarks` package only. The task partitioner,
`python plugins/harness2-benchmarks/scripts/split_tasks.py`, requires this source tree. For a wheel
installation, work from a dedicated directory: `.harness2/.env`, `~/.cache/harness2/benchmarks/`, and
`.harness2/runs/` are resolved there.
