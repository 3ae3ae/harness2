# Changelog

All notable changes to Harness² CLI are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

The first release of Harness² CLI, a general-purpose version of
[google-research/harness2](https://github.com/google-research/harness2) (forked at
`1cfe254`). The method and its prompts are unchanged; everything around them is new.

### Added

- One `harness2` command with subcommands: `init`, `doctor`, `task`, `run`, `status`,
  `stop`, `diff`, `eval`, `apply`, `judge`, `report`, `runs`, `config`, `chat`, `guide`, `login`,
  `setup`, `demo`, `bridge`, `completion`.
- Your own tasks: `.harness2/tasks/<id>/task.md` with optional `task.toml`, run in a clean
  git worktree of your repository or in a scratch directory, graded optionally by a `check`
  command or a `rubric.md`.
- Claude Code and command executors. A project's own harness (CLAUDE.md, `.claude/skills`,
  `.claude/agents`, `.claude/scripts`, hooks; or AGENTS.md, `.agents/`, `.codex/hooks.json`)
  is imported as the starting harness.
- Subscriptions: `claude-login/<model>` runs on Claude Code's own login and
  `chatgpt-login/<model>` on a Codex login kept for harness2 (`harness2 login codex`), with
  personal settings, hooks, plugins and instructions kept out of the runs.
- Any model provider: Anthropic, OpenAI, OpenAI- or Anthropic-compatible servers, Vertex AI
  and Bedrock, through provider profiles; `harness2 bridge` serves any of them to Codex.
- Layered settings: defaults, `~/.config/harness2/config.toml`, `.harness2/config.toml`,
  profiles, `HARNESS2_<SECTION>__<KEY>` variables and flags, with `config show --origin`.
- One final harness per run, built from every task's adopted harness and lesson;
  `harness2 eval` compares it with the starting harness, and `harness2 apply` writes it into
  the project, keeping edits made since the run started.
- Run control: `--dry-run` call estimates, `--budget-usd` and `--max-calls` limits,
  clean stops at a provider's usage limit, Ctrl-C and `harness2 stop` with resume,
  `--detach` background runs, relocatable run directories, `events.jsonl`.
- Use by AI agents: `--json` for every command including errors, flags for every prompt,
  and `harness2 guide` (also installable as a Claude Code skill).
- Interactive use: the `init` wizard, `status --watch`, `apply -i`, `--selector human`,
  `harness2 chat`.
- CI on Linux and macOS with Python 3.12 and 3.13.

### Changed

- The distribution is `harness2-cli`; the command and the import package stay `harness2`.
- The agent CLIs are the ones installed on the system instead of pinned downloads.
- Codex harness scripts live in `.agents/scripts/`, so they never collide with a project's
  `scripts/`.

### Removed

- The seven `harness2-*` scripts' old flags (`--bench`, `--substrate`); the script names
  remain as aliases of the new subcommands.
- The Vertex-only configuration in `.env` from the core package. JobBench, LAB and
  WorkBuddy-Bench moved, with that configuration, to the
  [harness2-benchmarks plugin](plugins/harness2-benchmarks/).
