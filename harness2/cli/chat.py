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

"""Talk to your agent CLI about this project's harness2 setup: tasks, runs and results.

Starts Claude Code or Codex interactively in the project with a guide to harness2 and the
project's current state added to its instructions. It is your own session, with your own
CLI login and settings; harness2 only adds the guide.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

GUIDE = """\
You are helping a person use Harness² (the `harness2` command) in the project at {root}.

Harness² improves the harness of a coding agent -- the files the agent reads on every run:
CLAUDE.md or AGENTS.md, skills, subagents, helper scripts and hooks. It runs the agent on the
person's own tasks, has an improver propose harness edits from what those runs show, probes
the edits on the same tasks, and composes the best of them. A run never changes the project's
files; `harness2 apply` writes an adopted harness back once the person has reviewed it.

How you help:
- Writing tasks. A task is a directory .harness2/tasks/<id>/ holding task.md -- the
  instruction the agent receives, written the way the person would really ask -- and an
  optional task.toml with: domain, split ("train" or "test"), workspace ("project": a clean
  checkout of the repository at `ref`; "files": a scratch directory seeded from files/),
  ref, deliverable ("diff", "files" or "message"), check (a shell command that grades the
  result), timeout. check/ and rubric.md hold grading material that neither the agent nor
  the improver ever sees. Good tasks are real, varied work the person wants done better; a
  check command or a rubric.md makes the results measurable.
- Reading results. A run lives in .harness2/runs/<name>/: run.json (its settings),
  events.jsonl (progress and cost), adapt/<namespace>/<task>/ (summary.json, the candidate
  harnesses under harnesses/, and the selection/ and reflection/ notes) and library/ (what
  carries over between tasks in the same domain). `harness2 status`, `harness2 report` and
  `harness2 diff` summarise them.
- Settings. `harness2 config show --origin` prints every setting and where it came from;
  `harness2 config set KEY VALUE` changes one.

Rules:
- `harness2 run`, `harness2 judge` and `harness2 doctor --live` call models and cost money.
  Propose the exact command and let the person run or approve it.
- Never edit anything under .harness2/runs/.
- Do not edit the project's harness files to apply a result; `harness2 apply` does that
  after review.

Current state of this project:
{state}
"""


def add_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "chat",
        help="talk to your agent CLI about tasks, runs and results",
        description="Start Claude Code or Codex in this project with a guide to harness2 "
        "added to its instructions.",
    )
    p.add_argument("--with", dest="tool", choices=("claude", "codex"), help="which CLI to start")
    return p


def run(args, ctx) -> int:
    tool = args.tool or _default_tool(ctx.settings)
    binary = shutil.which(tool) if tool else None
    if not binary:
        print("Neither `claude` nor `codex` is on PATH.", file=sys.stderr)
        return 1
    guide = GUIDE.format(root=ctx.project, state=_state(ctx))
    if tool == "claude":
        argv = [binary, "--append-system-prompt", guide]
    else:
        argv = [binary, "-c", f"developer_instructions={json.dumps(guide)}"]
    os.chdir(ctx.project)
    os.execv(binary, argv)
    return 1  # unreachable


def _default_tool(settings) -> str | None:
    for tool in (settings.get("improver.channel"), settings.get("executor.name"), "claude"):
        if tool in ("claude", "codex") and shutil.which(tool):
            return tool
    return "codex" if shutil.which("codex") else None


def _state(ctx) -> str:
    s = ctx.settings
    lines = [
        f"- executor: {s.get('executor.name')} on {s.get('executor.model') or '(model not set)'}",
        f"- improver: {s.get('improver.channel')} on {s.get('improver.model') or '(not set)'}",
        f"- harness directory: {s.get('paths.harness')}",
    ]
    tasks_dir = s.path("paths.tasks")
    tasks = sorted(p.parent.name for p in tasks_dir.glob("*/task.md"))
    shown = ", ".join(tasks[:30]) + (" ..." if len(tasks) > 30 else "")
    lines.append(f"- tasks ({len(tasks)}): {shown or 'none yet'}")
    runs_dir = s.path("paths.runs")
    runs = sorted(
        (p for p in runs_dir.glob("*/run.json")), key=lambda p: p.stat().st_mtime, reverse=True
    )
    lines.append(
        "- runs, newest first: " + (", ".join(p.parent.name for p in runs[:10]) or "none yet")
    )
    return "\n".join(lines)
