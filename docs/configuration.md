# Configuration

Harness² reads its settings from TOML files, environment variables and command-line flags.
Every setting has a default, so a project only needs the few keys that differ.

- [Where settings come from](#where-settings-come-from)
- [Models and providers](#models-and-providers)
- [Reference](#reference)
- [Profiles](#profiles)
- [Secrets](#secrets)

## Where settings come from

Later sources override earlier ones:

| Source | Location |
|---|---|
| Defaults | built in |
| Global file | `~/.config/harness2/config.toml` (or `$XDG_CONFIG_HOME/harness2/config.toml`) |
| Project file | `<project>/.harness2/config.toml` |
| Profile | a `[profiles.<name>]` table, selected with `--profile <name>` or `HARNESS2_PROFILE` |
| Environment | `HARNESS2_<SECTION>__<KEY>`, for example `HARNESS2_EXECUTOR__MODEL` |
| Command line | `-c key=value` (repeatable) and command flags such as `--k 2` |

The project is the nearest directory, upward from the current one, that contains
`.harness2/`. `-C <dir>` starts the search somewhere else. `harness2 init` creates
`.harness2/` with a starter `config.toml`.

To see the resolved settings and where each one comes from:

```bash
harness2 config show --origin
harness2 config get executor.model
harness2 config set run.workers 2            # edits .harness2/config.toml, keeps comments
harness2 config set --global providers.local.base_url http://localhost:8000/v1
```

Values given in the environment or with `-c` are read as TOML literals, so `-c run.k=2` is
a number and `-c 'executor.editable=["prompts/*.md"]'` is a list. Anything that does not
parse as TOML is taken as a plain string: `-c executor.model=openai/gpt-6.1-sol`.

## Models and providers

Every model is written `provider/model`. The provider part names a `[providers.<name>]`
table, which says what kind of endpoint it is and how it is reached.

### Subscriptions

Two built-in providers use the login your installed CLI already has, so a subscription is
all you need:

| Model | Runs on |
|---|---|
| `claude-login/<model>`, e.g. `claude-login/claude-sonnet-5-5` | your Claude subscription, through Claude Code's login (`claude auth login`) |
| `chatgpt-login/<model>`, e.g. `chatgpt-login/gpt-6.1-sol` | your ChatGPT subscription, through a Codex login kept for harness2 (`harness2 login codex`, once) |

`harness2 init` picks these by default, and `harness2 login` and `harness2 doctor` say
whether they are ready. Runs keep your personal setup out of the harness being measured:

- **Claude Code** runs on its own login, with `--setting-sources project,local
  --strict-mcp-config --no-session-persistence`: your `~/.claude/CLAUDE.md`, skills, plugins,
  hooks, MCP servers and settings are not loaded, and no session history is written.
- **Codex** cannot switch off a personal `~/.codex/hooks.json` or `~/.codex/AGENTS.md` with
  a flag, so harness2 gives it a home of its own, `~/.local/state/harness2/codex-home`.
  `harness2 login codex` signs that home in to ChatGPT once. It is a separate login: your own
  `~/.codex` is never read or changed. Runs also pass `--ignore-user-config --ignore-rules`.

To run Codex on your own home anyway, with whatever hooks and instructions it holds, set
its path; `harness2 doctor` then lists the personal files every run will include:

```toml
[providers.chatgpt-login]
home = "~/.codex"
```

Subscriptions have usage limits. When a run reaches one, it stops cleanly (exit code 4),
keeps everything finished so far, and the same `harness2 run` command resumes once the
limit resets. With a subscription, keep `run.phase_width` and `run.proposer_concurrency`
low, and cap runs with `--max-calls`.

### API keys and other endpoints

Two API-key providers are built in:

```toml
[providers.anthropic]
type = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"

[providers.openai]
type = "openai"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
```

So `anthropic/claude-sonnet-5-5` and `openai/gpt-6.1-sol` work as soon as the matching key
is set. Add your own provider for anything else:

```toml
# An OpenAI-compatible server: vLLM, LM Studio, Ollama, a company gateway ...
[providers.local]
type = "openai"
base_url = "http://localhost:8000/v1"
api_key_env = "LOCAL_API_KEY"

# An Anthropic-compatible gateway
[providers.gateway]
type = "anthropic"
base_url = "https://llm-gateway.example.com"
api_key_env = "GATEWAY_KEY"

# Google Vertex AI (uses Application Default Credentials)
[providers.vertex]
type = "vertex"
project = "my-gcp-project"
region = "global"

# Amazon Bedrock (uses the usual AWS credentials)
[providers.bedrock]
type = "bedrock"
region = "us-east-1"
```

Each agent CLI speaks to some provider types and not others:

| Provider type | Claude Code (`claude`) | Codex (`codex`) | OpenCode (`opencode`) |
|---|---|---|---|
| `claude-login` | yes | no | no |
| `chatgpt-login` | no | yes | no |
| `anthropic` | yes | via `harness2 bridge` | yes |
| `openai` | no | yes (Responses API) | yes (Chat Completions) |
| `vertex` | yes | via `harness2 bridge` | yes |
| `bedrock` | yes | via `harness2 bridge` | no |

`harness2 doctor` reports a model whose provider its CLI cannot use. To run Codex on a model
it cannot reach directly, serve that model as a Responses API endpoint with the bridge
(`pip install 'harness2-cli[bridge]'`), then point an `openai`-type provider at it:

```bash
harness2 bridge --model anthropic/claude-sonnet-5-5
```

The agent CLIs are the ones installed on your system (`claude`, `codex`, `opencode` on
`PATH`). Set `executor.bin` or `improver.bin` to use another binary or a full command.

With an API-key provider, every run also gets a fresh, empty configuration directory
(`CLAUDE_CONFIG_DIR` for Claude Code, `CODEX_HOME` for Codex), the strictest isolation.
`harness2 chat` is different from runs: it starts your own CLI exactly as you normally use
it.

## Reference

### `[executor]`: the agent that runs the tasks

| Key | Default | Meaning |
|---|---|---|
| `name` | `"claude"` | `claude`, `codex` or `command`. Plugin suites accept their own executors (the benchmark plugin uses `opencode` and `codex`). |
| `model` | `""` | `provider/model` the executor runs on. Required for `claude` and `codex`. |
| `bin` | `""` | Binary or command to start the CLI. Empty means the executor name on `PATH`. |
| `effort` | `"high"` | Reasoning effort passed to the CLI. |
| `timeout` | `1800` | Seconds one rollout may take. A task's `timeout` overrides it. |
| `command` | `""` | `command` executor only: the shell command that runs one task. `{workspace}`, `{prompt_file}` and `{output}` are replaced with quoted paths. |
| `editable` | `[]` | `command` executor only: glob patterns, relative to `paths.harness`, of the files the improver may edit. |

### `[improver]`, `[selector]`, `[reflector]`, `[judge]`: the agents that work on the harness

The improver proposes and composes harness edits. The selector picks the final harness when
several candidates compete (only when `k` > 1). The reflector writes the lesson that carries
over to later tasks. The judge grades tasks that have a `rubric.md`.

`[improver]` holds the defaults for all four. `[selector]` and `[judge]` override the
improver; `[reflector]` overrides the selector, or the improver when the selector is a
person.

| Key | Default | Meaning |
|---|---|---|
| `channel` | `"claude"` | Which CLI runs the session: `claude`, `codex` or `opencode`. `[selector]` also accepts `human`. |
| `model` | `""` | `provider/model`. Required for the improver. |
| `bin` | `""` | Binary or command for the channel's CLI. |
| `effort` | `"high"` | Reasoning effort. |
| `timeout` | `3600` | Seconds one session may take. |
| `steps` | `120` | Step limit for OpenCode sessions. |
| `steer` | `""` | Improver only: guidance added to every improver session, as text or `@path/to/file`. Describe what you want from the agent's work, not how it will be measured. |

### `[paths]`

| Key | Default | Meaning |
|---|---|---|
| `harness` | `"."` | Where the harness files live, relative to the project root. |
| `tasks` | `".harness2/tasks"` | The task directory. |
| `runs` | `".harness2/runs"` | Where runs are stored. |

### `[run]`

| Key | Default | Meaning |
|---|---|---|
| `k` | `1` | Task-level recursion budget. See [how it works](how-it-works.md#scaling-with-k). |
| `modes` | `"parallel"` | `parallel`, `sequential`, or both separated by a comma. |
| `seed` | unset | Shuffles the task order within each domain. Unset keeps sorted order. |
| `workers` | `1` | Domains run at the same time. |
| `phase_width` | `2` | Rollouts and improver sessions run at the same time within one task. |
| `proposer_concurrency` | `2` | Model sessions running at the same time across the whole run. |
| `transport_attempts` | `2` | Attempts per improver session before it counts as failed. |
| `obs_trunc` | `50000` | Characters of each tool output kept when trajectories are shown to the improver. |
| `budget_usd` | `0` | Stop the run once it has spent this many dollars. `0` means no limit. |
| `max_calls` | `0` | Stop the run after this many model calls (rollouts plus sessions). `0` means no limit. |

Spending is counted from the costs the CLIs report. Claude Code and OpenCode report dollars;
Codex reports tokens only, so `budget_usd` does not see Codex spending, and with a
subscription the reported dollars are an estimate rather than a charge. Use `max_calls` to
bound those runs. A call already in flight when a limit is reached still finishes.

### `[suite]`

| Key | Default | Meaning |
|---|---|---|
| `name` | `"tasks"` | Which suite supplies the tasks: `tasks` (your own) or a suite installed by a plugin. |
| `work` | `""` | One sentence describing the work, shown to the improver. For example: "maintain a Django web service and its test suite". |

## Profiles

A profile is a named set of overrides you switch on with `--profile`. Put profiles in the
global file to share them between projects:

```toml
[profiles.cheap.executor]
model = "anthropic/claude-haiku-5-5"

[profiles.cheap.improver]
model = "anthropic/claude-sonnet-5-5"

[profiles.cheap.run]
max_calls = 200
```

```bash
harness2 run --profile cheap
```

## Secrets

Keys are never written into config files. A provider names the environment variable that
holds its key (`api_key_env`), and a config file that contains a literal `api_key` is
rejected. Put project-specific keys in `.harness2/.env`, which `harness2 init` adds to
`.harness2/.gitignore`:

```bash
# .harness2/.env
ANTHROPIC_API_KEY=sk-ant-...
```

Variables already set in your shell win over `.harness2/.env`.
