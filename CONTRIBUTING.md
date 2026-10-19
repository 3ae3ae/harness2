# Contributing

Issues and pull requests are welcome. Harness² CLI is an independent fork of
[google-research/harness2](https://github.com/google-research/harness2); changes here are
not sent upstream. Every pull request runs the tests and ruff in CI, and notable changes go
in [CHANGELOG.md](CHANGELOG.md) under "Unreleased".

## Development setup

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e . -e plugins/harness2-benchmarks
python -B -m unittest discover -s tests
python -B -m unittest discover -s plugins/harness2-benchmarks/tests
ruff check harness2 tests plugins && ruff format --check harness2 tests plugins
```

The tests run offline: they use the deterministic demo adapter and small fake `claude` /
`codex` executables, never a real model. `harness2 demo` exercises the whole method locally
in a few seconds.

## Layout

| Path | Contents |
|---|---|
| `harness2/core/` | The method: recursion modes, prompts, evidence, selection, library, scheduler, events |
| `harness2/substrate/` | Executors (Claude Code, Codex, OpenCode, command): editable surfaces, native harness import/export, trajectory renderers |
| `harness2/suite.py`, `harness2/workspace.py` | The built-in `tasks` suite: tasks from `.harness2/tasks/`, rollouts in git worktrees or scratch directories |
| `harness2/settings.py`, `harness2/providers.py`, `harness2/registry.py` | Layered settings, provider profiles, suite discovery |
| `harness2/cli/` | The `harness2` command, one module per subcommand |
| `plugins/harness2-benchmarks/` | The paper's benchmarks as a separate distribution |
| `docs/` | User documentation |

A new suite is a module exposing `make_adapter(ctx)` (returning a
`harness2.core.spec.BenchmarkAdapter`) and, optionally, `check(ctx)`, `setup(argv)` and
`DESCRIPTION`, registered under the `harness2.suites` entry-point group. The benchmarks plugin
is a complete example.

## Conventions

- Keep changes small, and reuse what is already there before adding code. Prefer the
  standard library; the core package has no runtime dependencies.
- New behaviour comes with an offline test.
- Text that a model reads (prompts in `harness2/core/prompts.py`, suite prompt profiles) is
  part of the method: change it deliberately, and say why in the pull request.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`).
- Run the tests and `ruff` before opening a pull request, and describe how you tested.
