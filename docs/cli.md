# Command reference

```
harness2 [global options] <command> [options]
```

Every command takes `--help`. `python -m harness2` is the same as `harness2`.

- [Global options](#global-options)
- [Commands](#commands)
- [Exit codes](#exit-codes)
- [A run on disk](#a-run-on-disk)
- [Shell completion](#shell-completion)

## Global options

These go before the command: `harness2 -C ../api --json status`.

| Option | Meaning |
|---|---|
| `-C DIR`, `--project DIR` | Act as if started in `DIR`. The project is the nearest directory upward containing `.harness2/`. |
| `--profile NAME` | Apply the `[profiles.NAME]` settings. |
| `-c KEY=VALUE`, `--config KEY=VALUE` | Override one setting for this command; repeatable. See [Configuration](configuration.md). |
| `-q`, `--quiet` | Print results only, no progress. |
| `-v`, `--verbose` | Print debug details. |
| `--json` | Print results as JSON on stdout; progress still goes to stderr. A failing command prints `{"error": "...", "exit_code": n}` instead of an error line. |
| `--version` | Print the version. |

## Commands

### `init`

Set up harness2 in a project: asks which agent runs your tasks (`claude`, `codex` or a
custom `command`), whether it reaches its models through the CLI's own login (your
subscription) or an API key, which models to use, and where the harness lives; writes
`.harness2/config.toml`, `.harness2/.gitignore` and `.harness2/tasks/`.

| Option | Meaning |
|---|---|
| `--yes`, `-y` | Take every default without asking. Without a terminal, defaults are taken anyway. |
| `--executor {claude,codex,command}` | Skip the executor question. |
| `--auth {login,api-key}` | Skip the login question. |
| `--force` | Overwrite an existing config file. |

### `doctor`

Check that a run has what it needs: the suite and its tasks, each role's model and
provider, the agent CLIs, and provider keys. Makes no model call unless `--live`.

| Option | Meaning |
|---|---|
| `--live` | Also call each configured model once, with a tiny request. |

### `task new | ls | import`

```bash
harness2 task new ID [--files] [--domain D] [--check CMD]
harness2 task ls
harness2 task import FILE [--force]
```

`new` creates `.harness2/tasks/ID/` with a `task.md` to fill in and a `task.toml`. `--files`
makes a files task (with a `files/` directory) instead of a project task. `import` reads a
`.jsonl` or `.csv` file; `--force` overwrites existing tasks. See [Tasks](tasks.md).

### `run`

Refine the harness on the tasks, then combine what the tasks taught into one final harness
per recursion mode (one more improver session each), stored in the run's `consolidated/`
directory. Running the same command again resumes the run and reuses every finished step;
if building the final harness failed, only that step runs again.

| Option | Meaning |
|---|---|
| `--name NAME` | Run name. Default: `<executor>-k<k>`, plus `-s<seed>` with a seed. |
| `--k K` | Recursion budget per task (default 1). |
| `--mode MODES` | `parallel` (default), `sequential`, or `parallel,sequential`. |
| `--executor NAME` | Override `executor.name`. |
| `--suite NAME` | Task suite: `tasks` (default) or one installed by a plugin. |
| `--split {all,train,test}` | Run one split only. |
| `--only-domains D,...` | Run only these domains. |
| `--only-tasks ID,...` | Run only these tasks. |
| `--max-tasks N` | Run at most N tasks per domain. |
| `--seed N` | Shuffle the task order within each domain. |
| `--workers N` | Domains to run at the same time. |
| `--budget-usd USD` | Stop once the run has spent this much. |
| `--max-calls N` | Stop after N model calls. |
| `--selector human` | Pick the final harness yourself when `k` > 1. |
| `--steer TEXT\|@FILE` | Guidance added to every improver session. |
| `--dry-run` | Show the tasks and the number of model calls; run nothing. |
| `--detach` | Start the run in the background and return at once with its name, pid and log file (`run.log` in the run directory). |
| `--split-file FILE` | Task partition file, for benchmark suites. |

A run keeps the settings it started with. Resuming it with a different executor, model,
improver or task list is refused, listing what changed; use a new `--name` for a new
experiment. The limits apply to the whole run, across resumes. A run that is already going
is not started twice.

### `stop [NAME]`

Stop a running run, whether it was started with `--detach` or in another terminal. It
finishes the model calls in flight and exits; running the same `harness2 run` command again
resumes it.

### `status [NAME]`

Each task's state, the harness picked for it, rollouts done, and the run's spend. Works
while the run is going, from another terminal. NAME defaults to the most recent run.

| Option | Meaning |
|---|---|
| `--watch [SECONDS]` | Redraw every SECONDS (default 5) until Ctrl-C. |

### `judge [NAME]`

Grade the result each task adopted, with the task's `check` command or `rubric.md`. Tasks
with neither are skipped. Grades are written to the run and reused until the check, the
rubric or the judge model changes.

| Option | Meaning |
|---|---|
| `--bases` | Grade the runs of the starting harness instead, for comparison. |

### `report [NAME ...]`

Average the grades per recursion mode. Several names compare runs.

### `runs ls | show NAME | rm NAME`

List runs with their progress and spend, show one run's state and settings, or delete a run
and everything it stored (`--yes` to skip the question).

### `diff [NAME]`

Show how the run's final harness differs from the harness the run started from.

| Option | Meaning |
|---|---|
| `--task ID` | Show the harness adopted for this one task instead of the final harness. |
| `--mode {parallel,sequential}` | The recursion mode to take it from (default parallel). |
| `--stat` | List the changed files only. |

### `eval [NAME]`

Run the agent with the starting harness and with the final harness on the run's tasks that
have a check or rubric, grade both, and compare the pass rates. Costs one agent run per
task, harness and repeat.

| Option | Meaning |
|---|---|
| `--repeat N` | Runs per task and harness (default 1). More runs, steadier numbers. |
| `--split {all,train,test}` | Compare on one split, for example the `test` tasks the run did not use. |
| `--only-tasks ID,...` | Compare on these tasks only. |
| `--mode {parallel,sequential}` | Which mode's final harness to compare (default parallel). |

### `apply [NAME]`

Write the run's final harness into the project. Only what the run changed relative to the
harness it started from is applied, so edits you made since are kept; a file changed both
by the run and in the project is skipped unless `--force`. The changes are listed first.
With `--json`, `--yes` or `--dry-run` is required, since nothing is asked.

| Option | Meaning |
|---|---|
| `--task ID`, `--mode` | As for `diff`. |
| `-i`, `--interactive` | Show each file's diff and accept or skip it. |
| `-y`, `--yes` | Apply without asking. |
| `--branch NAME` | Create and switch to a git branch first. |
| `--dry-run` | Show the changes, write nothing. |
| `--force` | Also overwrite files changed in the project since the run started, and write over uncommitted changes. |

Only the executor's harness files are written. For Claude Code those are `CLAUDE.md`,
`.claude/skills/`, `.claude/agents/`, `.claude/scripts/` and the `hooks` key of
`.claude/settings.json` (its other keys are kept).

### `config show | get | set | path`

```bash
harness2 config show [--origin]       # every resolved setting, and where it came from
harness2 config get KEY
harness2 config set KEY VALUE [--global]
harness2 config path
```

`set` edits `.harness2/config.toml` (or the global file with `--global`) in place, keeping
comments. The value is read as a TOML literal when it parses: `run.k 2` is a number.

### `chat`

Start your own Claude Code or Codex in the project, with a guide to harness2 and the
project's tasks and runs added to its instructions. Ask it to help write tasks, a check or a
rubric, or to explain a run's results.

| Option | Meaning |
|---|---|
| `--with {claude,codex}` | Which CLI to start. Default: the improver's channel, if installed. |

### `login [codex]`

Without an argument, show whether `claude-login` (Claude Code's own login) and
`chatgpt-login` (the Codex login kept for harness2) are ready. `harness2 login codex` signs
Codex in to ChatGPT for harness2, in its own home, apart from your `~/.codex`; `--device`
uses a device code on machines without a browser.

### `guide`

Print the guide for AI agents that drive harness2, followed by the current project's
settings, tasks and runs. `--install claude` installs it as a user-level Claude Code skill
(`~/.claude/skills/harness2/`), which harness2's own runs never load.

### `setup SUITE [ARGS...]`

Run a plugin suite's installer, for example `harness2 setup lab` for the
[benchmarks plugin](../plugins/harness2-benchmarks/).

### `demo`

Run the method on two toy tasks with scripted editors: no model calls, a few seconds.
`--output DIR` (default `runs/demo`), `--k K` (default 2), `--mode {parallel,sequential,both}`.

### `bridge`

Serve a configured model as an OpenAI Responses API endpoint through LiteLLM, so the Codex
executor or channel can use providers it cannot reach directly. Needs
`pip install 'harness2-cli[bridge]'`.

| Option | Meaning |
|---|---|
| `--model PROVIDER/MODEL` | The model to serve (required). |
| `--host HOST` | Interface to listen on (default `127.0.0.1`). |
| `--port PORT` | Port. Default: the one used last time, or a free one. |

It prints the `[providers.bridge]` table to add to your config and the key to put in
`.harness2/.env`.

### `completion {bash,zsh}`

Print a shell completion script; see [below](#shell-completion).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | An error: bad settings, a missing CLI or key, a failed check, a run that could not continue. |
| 2 | Invalid command-line usage. |
| 3 | `run` finished, but some tasks failed. |
| 4 | `run` or `eval` stopped at `--budget-usd`, `--max-calls` or the provider's usage limit. Rerun to resume. |
| 130 | Interrupted with Ctrl-C. Rerun to resume. |

## A run on disk

```
.harness2/runs/<name>/
  run.json            settings, task list and schedule the run started with
  run.pid, run.log    while it runs: its process, and its output when started with --detach
  events.jsonl        one line per agent run, model session, selection and task, with costs
  consolidated/<mode>/ the final harness, the improver's notes and what it was built from
  eval/<mode>/        results of harness2 eval
  base_harness/       the project's harness when the run started
  rollouts/<id>/      one agent run: prompt.md, stream.jsonl (trajectory), final.md,
                      deliverable.patch or output/, meta.json, judge.json
  adapt/<namespace>/<task>/
    summary.json      what happened for the task
    harnesses/        every candidate harness
    selection/        the final pick and why
    reflection/       the lesson recorded for later tasks
  adapt/_audit/       every improver session's mission and reply
  library/            adopted harnesses and lessons per domain
```

Stored paths are relative, so a run directory can be moved or copied and still resumes.

## Shell completion

```bash
# zsh
harness2 completion zsh > "${fpath[1]}/_harness2"
# bash
mkdir -p ~/.local/share/bash-completion/completions
harness2 completion bash > ~/.local/share/bash-completion/completions/harness2
```
