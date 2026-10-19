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

from . import guide as G

CHAT_INTRO = """\
You are talking with a person about Harness² (the `harness2` command) in the project at
{root}. Help them write tasks, a check or a rubric, read a run's results, and decide what to
apply. The guide below is how to use harness2.

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
    if not sys.stdin.isatty():
        print("harness2 chat is a conversation and needs a terminal.", file=sys.stderr)
        return 1
    tool = args.tool or _default_tool(ctx.settings)
    binary = shutil.which(tool) if tool else None
    if not binary:
        print("Neither `claude` nor `codex` is on PATH.", file=sys.stderr)
        return 1
    guide = (
        CHAT_INTRO.format(root=ctx.project)
        + G.GUIDE
        + f"\n## This project\n\n{G.state_lines(ctx)}\n"
    )
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
