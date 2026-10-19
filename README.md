<h3 align="center">
  <img src="assets/Harness2-wordmark.png" alt="Harness²" width="360"><br>
  Harness² CLI: tune your coding agent's harness on your own tasks
</h3>

<p align="center">
  <a href="https://github.com/3ae3ae/harness2-cli/actions/workflows/ci.yml"><img src="https://github.com/3ae3ae/harness2-cli/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://arxiv.org/abs/2610.14224"><img src="https://img.shields.io/badge/Paper-arXiv-EA4335?logo=arxiv&logoColor=white" alt="Paper"></a>
  <a href="https://harness2.github.io"><img src="https://img.shields.io/badge/Website-harness2.github.io-4285F4?logo=googlechrome&logoColor=white" alt="Website"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-34A853" alt="License"></a>
</p>

A coding agent is a model plus a **harness**: the instructions, skills, subagents, helper
scripts and hooks it runs with. `harness2` improves the harness of the agent you already use
(Claude Code's `CLAUDE.md` and `.claude/`, or Codex's `AGENTS.md` and `.agents/`) by running
it on tasks from your own project, studying what happened, and refining the harness from
that evidence. The model stays the same. Your files change only when you apply the result.

Harness² CLI is a general-purpose version of
[Harness²: Recursive Agent Harnessing for an Open World](https://arxiv.org/abs/2610.14224).
The original release ([google-research/harness2](https://github.com/google-research/harness2))
reproduces the paper on three fixed benchmarks with Vertex AI. This fork turns the same method
into a command-line tool for any project, any task, Claude Code or Codex (or your own agent),
on your subscription or any model provider. [What changed](#what-changed-from-the-original)
lists the differences; the paper's benchmark setup lives on as a
[plugin](plugins/harness2-benchmarks/).

- [How it works](#how-it-works)
- [Install](#install)
- [Quick start](#quick-start)
- [Driving it from an agent](#driving-it-from-an-agent)
- [Commands](#commands)
- [Configuration](#configuration)
- [What changed from the original](#what-changed-from-the-original)
- [Documentation](#documentation)

## How it works

For every task you give it, `harness2`:

1. runs your agent with its current harness and records what it does;
2. has an improver agent propose harness edits from those trajectories, from two angles:
   how the agent works, and what it needs to know about this kind of task;
3. runs the agent with each candidate harness, and composes a refined one from what the
   differences between the runs show;
4. keeps the refined harness and a short lesson for the next task in the same area.

When all tasks are done, it combines what the tasks taught into **one final harness** for
your project. That is the single result you review with `harness2 diff` and write into the
project with `harness2 apply`.

No step needs a grade or a reference answer, so any task you can describe can be used. If
you can also check results, with a test command or a rubric, `harness2 eval` measures the
final harness against the one you started with. [How it works](docs/how-it-works.md) has
the details.

<p align="center">
  <a href="assets/harness2_pipeline.png">
    <img src="assets/harness2_pipeline.png" alt="Harness² pipeline: an agent runs the initial harness, an editor proposes general and domain-specific edits, candidate runs probe them, and a composer combines them into the final harness. A reflector then updates the experience carried to the next task." width="100%">
  </a>
</p>

## Install

You need Python 3.12 or 3.13, git, and the agent CLI whose harness you want to tune,
installed, on your `PATH` and logged in:
[Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`) or
[Codex](https://github.com/openai/codex) (`codex`).

```bash
uv tool install git+https://github.com/3ae3ae/harness2-cli
# or: pipx install git+https://github.com/3ae3ae/harness2-cli
```

By default runs use your subscription, so a Claude or ChatGPT plan is enough: Claude Code
runs on its own login, and Codex on a login kept for harness2 that you make once with
`harness2 login codex`. You
can use an API key or another provider instead; see [Configuration](#configuration). Runs
make many model calls and use money or subscription quota: the [quick start](#quick-start)
shows how to see the number of calls first and how to cap them.

## Quick start

Inside the project whose agent setup you want to improve:

```bash
harness2 init                          # pick the agent, how it logs in, where the harness lives
harness2 task new fix-login-redirect   # then write the request in .harness2/tasks/fix-login-redirect/task.md
harness2 doctor                        # check the CLI, its login and the tasks
harness2 run --dry-run                 # the tasks and how many model calls a run makes
harness2 run --max-calls 200           # refine the harness on every task, then build the final one
```

A run works in temporary git worktrees, so your working tree is never touched. While it
runs, from another terminal:

```bash
harness2 status --watch                # progress, spend, the harness picked per task
```

When it has finished:

```bash
harness2 diff                          # what the final harness changes
harness2 eval                          # optional: compare it with your current harness on graded tasks
harness2 apply                         # review the changes and write them into the project
```

Running `harness2 run` again resumes an interrupted or limited run and reuses every finished
step. Write a few varied tasks that look like the work you really do: see
[Tasks](docs/tasks.md).

## Driving it from an agent

`harness2` is built to be driven by an AI agent as well as by hand. Every command has
`--json`, errors included; no command waits for input without a terminal, since every prompt
has a flag; and long runs go to the background with `harness2 run --detach`, are followed with
`harness2 status --json` and are stopped with `harness2 stop`.

`harness2 guide` prints the contract for agents, with your project's current state. Tell your
agent to read it, or install it as a Claude Code skill:

```bash
harness2 guide --install claude
```

## Commands

| Command | What it does |
|---|---|
| `harness2 init` | Set up `.harness2/` in a project, with a short wizard (`--yes` for defaults) |
| `harness2 doctor` | Check the agent CLIs, their login or keys, settings and tasks; `--live` also makes one tiny call per model |
| `harness2 task new / ls / import` | Create, list and import tasks |
| `harness2 run` | Refine the harness on the tasks and build the final harness; `--dry-run` previews, `--max-calls` / `--budget-usd` cap spending, `--detach` runs in the background, `--selector human` lets you pick between candidates |
| `harness2 status` | A run's progress; `--watch` keeps it updated |
| `harness2 stop` | Stop a running run; running it again resumes |
| `harness2 diff` | What the final harness changes |
| `harness2 eval` | Compare the final harness with the starting one on graded tasks |
| `harness2 apply` | Write the final harness into the project after showing the changes; `-i` to accept file by file |
| `harness2 judge` / `report` | Grade each task's adopted result, and summarise the grades |
| `harness2 runs ls / show / rm` | List, inspect and delete runs |
| `harness2 config show / get / set` | Read and change settings; `show --origin` says where each value comes from |
| `harness2 login` | Check the subscription logins; `login codex` signs Codex in for harness2 |
| `harness2 chat` | Talk to your agent CLI about tasks, runs and results |
| `harness2 guide` | The guide for AI agents that use harness2 |
| `harness2 demo` | A local run with scripted editors and no model calls |

Every command takes `--help`. The [command reference](docs/cli.md) lists every flag, the exit
codes and the layout of a run directory.

## Configuration

Settings come from `~/.config/harness2/config.toml`, the project's `.harness2/config.toml`,
`HARNESS2_*` environment variables and flags, in that order of precedence. Models are
written `provider/model`:

```toml
[executor]
name = "claude"
model = "claude-login/claude-sonnet-5-5"   # your Claude subscription, through Claude Code

[improver]
channel = "claude"
model = "claude-login/claude-opus-5-5"
```

`chatgpt-login/<model>` uses your ChatGPT subscription through Codex (after
`harness2 login codex`). API keys and other
endpoints work too: Anthropic, OpenAI, any OpenAI-compatible or Anthropic-compatible
server, Vertex AI and Bedrock. To tune an agent other than Claude Code or Codex, the
`command` executor runs your own command and edits the files you list.
[Configuration](docs/configuration.md) covers every setting.

## What changed from the original

| | google-research/harness2 | Harness² CLI |
|---|---|---|
| Tasks | JobBench, LAB, WorkBuddy-Bench | your own tasks, in your repository or from files; the benchmarks as a plugin |
| Agents | OpenCode, Codex, pinned builds | Claude Code, Codex or any command, as installed on your system |
| Models | Vertex AI | your Claude or ChatGPT subscription, or any API provider |
| Result | one adopted harness per task, kept for scoring | one final harness for the project, which you review and apply |
| Interface | seven scripts, settings in `.env` | one `harness2` command, layered TOML settings, JSON output, background runs, resumable limits |

The method itself (propose, probe, compose, and the library of lessons) and its prompts are
the paper's. [How it works](docs/how-it-works.md) describes what was added around it.

## Documentation

- [How it works](docs/how-it-works.md): the method, the final harness, what a run costs and stores
- [Tasks](docs/tasks.md): writing tasks, workspaces, grading
- [Configuration](docs/configuration.md): settings, subscriptions, models and providers
- [Command reference](docs/cli.md): every command and flag
- [Changelog](CHANGELOG.md) and [contributing](CONTRIBUTING.md)

## Citation

If you use Harness² CLI in research, cite the paper:

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
paper's authors at Google; Harness² CLI is an independent fork, not a Google product, and is
not maintained by Google. Third-party code in the benchmarks plugin keeps its upstream
licenses.
