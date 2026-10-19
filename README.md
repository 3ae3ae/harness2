<h3 align="center">
  <img src="assets/Harness2-wordmark.png" alt="Harness²" width="360"><br>
  Tune your coding agent's harness on your own tasks
</h3>

<p align="center">
  <a href="https://arxiv.org/abs/2610.14224"><img src="https://img.shields.io/badge/Paper-arXiv-EA4335?logo=arxiv&logoColor=white" alt="Paper"></a>
  <a href="https://harness2.github.io"><img src="https://img.shields.io/badge/Website-harness2.github.io-4285F4?logo=googlechrome&logoColor=white" alt="Website"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-34A853" alt="License"></a>
</p>

A coding agent is a model plus a **harness**: the instructions, skills, subagents, helper
scripts and hooks it runs with. `harness2` improves the harness of the agent you already use
(Claude Code's `CLAUDE.md` and `.claude/`, or Codex's `AGENTS.md` and `.agents/`) by running
it on tasks from your own project, studying what happened, and refining the harness from
that evidence. The model stays the same. Your files change only when you apply a result you
have reviewed.

It is a general-purpose command-line tool built on the method of
[Harness²: Recursive Agent Harnessing for an Open World](https://arxiv.org/abs/2610.14224).
This repository is an independent fork of
[google-research/harness2](https://github.com/google-research/harness2); the paper's
benchmark setup lives on as a [plugin](plugins/harness2-benchmarks/).

- [How it works](#how-it-works)
- [Install](#install)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Configuration](#configuration)
- [Other agents](#other-agents)
- [The paper's benchmarks](#the-papers-benchmarks)
- [Documentation](#documentation)

## How it works

For every task you give it, `harness2`:

1. runs your agent with its current harness and records what it does;
2. has an improver agent propose harness edits from those trajectories, from two angles:
   how the agent works, and what it needs to know about this kind of task;
3. runs the agent with each candidate harness, and composes a final one from what the
   differences between the runs show;
4. keeps the adopted harness and a short lesson for the next task in the same area.

No step needs a grade or a reference answer, so any task you can describe can be used.
If you can also check the result, with a test command or a rubric, `harness2 judge` measures
how much the adopted harness helped. [How it works](docs/how-it-works.md) has the details.

<p align="center">
  <a href="assets/harness2_pipeline.png">
    <img src="assets/harness2_pipeline.png" alt="Harness² pipeline: an agent runs the initial harness, an editor proposes general and domain-specific edits, candidate runs probe them, and a composer combines them into the final harness. A reflector then updates the experience carried to the next task." width="100%">
  </a>
</p>

## Install

You need Python 3.12 or 3.13, git, and the agent CLI whose harness you want to tune:
[Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`) or
[Codex](https://github.com/openai/codex) (`codex`), installed and on your `PATH`.

```bash
uv tool install git+https://github.com/3ae3ae/harness2
# or: pipx install git+https://github.com/3ae3ae/harness2
```

Runs call models through your provider's API, so set the key for the models you choose,
for example `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. Model calls cost money; the
[quick start](#quick-start) shows how to see the cost of a run before you start it.

## Quick start

Inside the project whose agent setup you want to improve:

```bash
harness2 init                          # pick the agent and models, creates .harness2/
harness2 task new fix-login-redirect   # then write the request in .harness2/tasks/fix-login-redirect/task.md
harness2 doctor                        # check CLIs, keys and tasks
harness2 run --dry-run                 # the task order and how many model calls a run makes
harness2 run                           # refine the harness on every task
```

A run works in temporary git worktrees, so your working tree is never touched. While it
runs, or after it stops:

```bash
harness2 status --watch                # progress, spend, the harness picked per task
harness2 judge && harness2 report      # grade the results of tasks that have a check or rubric
harness2 diff                          # what the adopted harness changes
harness2 apply                         # review it and write it into the project
```

Run `harness2 run` again to resume an interrupted run; finished steps are reused. Write a
few varied tasks that look like the work you really do: see [Tasks](docs/tasks.md).

## Commands

| Command | What it does |
|---|---|
| `harness2 init` | Set up `.harness2/` in a project, with a short interactive wizard |
| `harness2 doctor` | Check the agent CLIs, provider keys, settings and tasks; `--live` also makes one tiny model call per model |
| `harness2 task new / ls / import` | Create, list and import tasks |
| `harness2 run` | Refine the harness on the tasks; `--dry-run` previews, `--budget-usd` and `--max-calls` cap spending, `--selector human` lets you pick the final harness yourself |
| `harness2 status` | A run's progress; `--watch` keeps it updated |
| `harness2 judge` | Grade the result each task adopted |
| `harness2 report` | Summarise grades, per run |
| `harness2 runs ls / show` | List runs and inspect one |
| `harness2 diff` | Show how an adopted harness differs from the starting one |
| `harness2 apply` | Write an adopted harness into the project, after showing the changes; `-i` to accept file by file |
| `harness2 config show / get / set` | Read and change settings; `show --origin` says where each value comes from |
| `harness2 chat` | Talk to your agent CLI about tasks, runs and results |
| `harness2 demo` | A local run with scripted editors and no model calls |

Every command takes `--help`. See the [command reference](docs/cli.md) for all flags, exit
codes and the layout of a run directory.

## Configuration

Settings come from `~/.config/harness2/config.toml`, the project's
`.harness2/config.toml`, `HARNESS2_*` environment variables and flags, in that order of
precedence. Models are written `provider/model`; any OpenAI-compatible or
Anthropic-compatible endpoint, Vertex AI and Bedrock can be used:

```toml
[executor]
name = "claude"
model = "anthropic/claude-sonnet-5-5"

[improver]
channel = "claude"
model = "anthropic/claude-opus-5-5"

[providers.local]                # e.g. a vLLM or LM Studio server
type = "openai"
base_url = "http://localhost:8000/v1"
api_key_env = "LOCAL_API_KEY"
```

[Configuration](docs/configuration.md) lists every setting.

## Other agents

To tune an agent other than Claude Code or Codex, use the `command` executor: give it the
command that runs one task and the files that make up its harness.

```toml
[executor]
name = "command"
command = "my-agent --task {prompt_file} --workdir {workspace}"
editable = ["prompts/*.md", "agent.yaml"]
```

## The paper's benchmarks

JobBench, LAB and WorkBuddy-Bench, the benchmarks of the paper, are packaged as the
[harness2-benchmarks plugin](plugins/harness2-benchmarks/). Install it next to `harness2`
and they become suites: `harness2 run --suite jb`.

## Documentation

- [How it works](docs/how-it-works.md): the method, what a run costs, what it stores
- [Tasks](docs/tasks.md): writing tasks, workspaces, grading
- [Configuration](docs/configuration.md): settings, models and providers
- [Command reference](docs/cli.md): every command and flag
- [Contributing](CONTRIBUTING.md)

## Citation

```bibtex
@article{xu2026harness2,
  title   = {Harness$^2$: Recursive Agent Harnessing for an Open World},
  author  = {Xu, Ruiyao and Chen, Yanfei and CuiZhu, Zhongying and Dalvi Mishra, Bhavana and Ming, Yifei and Yu, Han and Han, Rujun and Lee, Chen-Yu and Pfister, Tomas},
  journal = {arXiv preprint arXiv:2610.14224},
  year    = {2026},
  url     = {https://arxiv.org/abs/2610.14224}
}
```

## License

Apache 2.0; see [LICENSE](LICENSE). The method and the original implementation are by the
paper's authors at Google; this fork is not a Google product and is not maintained by
Google. Third-party code in the benchmarks plugin keeps its upstream licenses.
