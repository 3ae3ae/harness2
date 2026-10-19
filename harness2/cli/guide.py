# Copyright 2026 The Harness2 Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The guide for AI agents that drive harness2 on a person's behalf.

`harness2 guide` prints it with the current project's state; `--install claude` installs it
as a user-level Claude Code skill, which harness2's own runs never load.
"""

from __future__ import annotations

import argparse
from pathlib import Path

GUIDE = """\
# harness2: guide for agents

harness2 improves a coding agent's harness -- the files the agent reads on every run:
CLAUDE.md or AGENTS.md, skills, subagents, helper scripts and hooks. It runs the agent on the
project's own tasks, proposes harness edits from what those runs show, probes them on the
same tasks, and ends with one final harness for the project. A run never changes the
project's files; `harness2 apply` writes the final harness back once the person agrees.

## Rules

- Runs spend money or subscription quota. Before `harness2 run` or `harness2 eval`, show the
  person the plan from `harness2 run --dry-run` and get their go-ahead. Bound every run with
  `--max-calls N` (and `--budget-usd X` when an API key pays for it).
- Read results with `--json`. Errors then arrive on stdout as
  {"error": "...", "exit_code": n}.
- Nothing waits for keyboard input when there is no terminal: answer each prompt with its
  flag (`--yes`, `--auth`, `--executor`, ...) instead.
- Never edit anything under .harness2/runs/. Never edit the project's harness files to apply
  a result: show `harness2 diff`, then run `harness2 apply --yes` once the person agrees.

## Workflow

1. `harness2 init --yes [--executor claude|codex] [--auth login|api-key]` creates
   .harness2/. `--auth login` uses a Claude or ChatGPT subscription: Claude Code's own login
   as it is, or for Codex a separate login the person makes once with `harness2 login codex`
   (it opens a browser; ask them to run it). `harness2 login --json` shows which are ready. Change any choice afterwards with `harness2 config set KEY VALUE`, for
   example `executor.model`.
2. Tasks. `harness2 task new <id> [--files] [--check "<cmd>"]`, then write the request in
   .harness2/tasks/<id>/task.md the way the person would really ask for it. Good tasks are
   real, varied work the person wants done better. A `check` command (exit status 0 means
   pass) makes results measurable; grading files in check/ and rubric.md are never shown to
   the agent. More options in task.toml: domain, split ("train"/"test"), workspace
   ("project": a clean checkout at `ref`; "files": a scratch directory seeded from files/),
   deliverable ("diff", "files", "message"), timeout. List them with
   `harness2 task ls --json`.
3. `harness2 doctor --json`: every row must be ok before a run.
4. `harness2 run --dry-run` shows the tasks and the number of model calls. After the
   person's go-ahead: `harness2 run --detach --max-calls N`. It returns at once with the run
   name; runs take minutes to hours.
5. Follow it with `harness2 status <name> --json`: "active", each task's "state",
   "spent_usd", "calls". `harness2 stop <name>` stops it; running the same `harness2 run`
   command again resumes and reuses every finished step.
6. When it has finished, `harness2 diff <name>` shows the final harness. To measure it
   first, `harness2 eval <name> --json` compares it with the starting harness on the tasks
   that have a check or rubric.
7. With the person's agreement: `harness2 apply <name> --yes`.

## Where things are

- .harness2/config.toml: settings; `harness2 config show --origin --json` resolves them.
- .harness2/runs/<name>/: run.json (settings, task list), events.jsonl (every agent run and
  model session, with cost), consolidated/<mode>/ (the final harness and the improver's
  notes), adapt/ (per-task candidates, selections, lessons), eval/ (eval results).

## Exit codes

0 ok, 1 error, 2 bad usage, 3 finished with failed tasks, 4 stopped at a limit (--max-calls,
--budget-usd, or the provider's usage limit), 130 interrupted. After 4 or 130, the same
command resumes.
"""

SKILL_HEAD = """\
---
name: harness2
description: Use harness2 when the person wants their coding agent's setup (CLAUDE.md, AGENTS.md, skills, subagents, hooks) improved on their own tasks, or asks to set up, run, follow, evaluate or apply a harness2 run.
---

"""


def add_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "guide",
        help="print the guide for AI agents that use harness2",
        description="Print how an AI agent should drive harness2, with this project's current "
        "state. Point your agent at it, or install it as a Claude Code skill.",
    )
    p.add_argument(
        "--install",
        choices=("claude",),
        help="install the guide as a user-level Claude Code skill (~/.claude/skills/harness2)",
    )
    return p


def run(args, ctx) -> int:
    if args.install:
        path = Path.home() / ".claude/skills/harness2/SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SKILL_HEAD + GUIDE, encoding="utf-8")
        ctx.out(
            {"installed": str(path)},
            lambda data: (
                f"Installed {data['installed']}. Claude Code loads it in new "
                "sessions; harness2's own runs never do."
            ),
        )
        return 0
    state = state_lines(ctx) if ctx.project_or_none else ""
    text = GUIDE + (f"\n## This project\n\n{state}\n" if state else "")
    ctx.out({"guide": GUIDE, "project": state or None}, lambda data: text.rstrip())
    return 0


def state_lines(ctx) -> str:
    """The project's settings, tasks and runs, in a few lines an agent can act on."""
    s = ctx.settings
    lines = [
        f"- root: {ctx.project}",
        f"- executor: {s.get('executor.name')} on {s.get('executor.model') or '(model not set)'}",
        f"- improver: {s.get('improver.channel')} on {s.get('improver.model') or '(not set)'}",
        f"- harness directory: {s.get('paths.harness')}",
    ]
    tasks = sorted(p.parent.name for p in s.path("paths.tasks").glob("*/task.md"))
    shown = ", ".join(tasks[:30]) + (" ..." if len(tasks) > 30 else "")
    lines.append(f"- tasks ({len(tasks)}): {shown or 'none yet'}")
    runs = sorted(
        s.path("paths.runs").glob("*/run.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    lines.append(
        "- runs, newest first: " + (", ".join(p.parent.name for p in runs[:10]) or "none yet")
    )
    return "\n".join(lines)
