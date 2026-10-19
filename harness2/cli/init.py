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

"""Set up harness2 in a project: pick the agent, its models and where the harness lives."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .. import settings as S
from ..core import tree as HT

DEFAULTS = {
    "claude": ("anthropic/claude-sonnet-5-5", "claude", "anthropic/claude-opus-5-5"),
    "codex": ("openai/gpt-6.1-sol", "codex", "openai/gpt-6.1-sol"),
}

CONFIG = """\
# Harness² project settings. See docs/configuration.md for every key;
# `harness2 config show --origin` prints the resolved values and where each comes from.

[executor]
# The agent CLI that runs your tasks, and the model it runs on (provider/model).
name = {name}
model = {model}
{command_lines}
[improver]
# The agent that studies the runs and edits the harness.
channel = {channel}
model = {improver}

# The selector picks the final harness when several candidates compete, and the judge
# grades tasks that have a rubric.md. Both use the improver's settings unless set here.
# [selector]
# model = "anthropic/claude-sonnet-5-5"

[paths]
# Where the harness lives, relative to the project root.
harness = {harness}
"""

COMMAND_LINES = """\
# The command that runs one task. {{workspace}}, {{prompt_file}} and {{output}} are filled in.
command = {command}
# The files the improver may edit, as glob patterns relative to paths.harness.
editable = {editable}
"""


def add_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "init",
        help="set up harness2 in this project",
        description="Create .harness2/ with a config file, a tasks directory and a .gitignore. "
        "Asks a few questions; --yes takes the defaults.",
    )
    p.add_argument("--yes", "-y", action="store_true", help="take every default, ask nothing")
    p.add_argument("--force", action="store_true", help="overwrite an existing config file")
    p.add_argument("--executor", choices=("claude", "codex", "command"), help="skip that question")
    return p


def run(args, ctx) -> int:
    root = Path(args.project or Path.cwd()).resolve()
    config = S.project_path(root)
    if config.exists() and not args.force:
        print(f"{config} already exists; pass --force to overwrite it.", file=sys.stderr)
        return 1
    ask = _asker(interactive=not args.yes and sys.stdin.isatty())
    print(f"Setting up Harness² in {root}\n")

    interactive = not args.yes and sys.stdin.isatty()
    found = {name: _version(name) for name in ("claude", "codex")}
    default = next((name for name in ("claude", "codex") if found[name]), None)
    executor = args.executor or (_pick_executor(ask, found, default) if interactive else default)
    if executor is None:
        print(
            "Neither Claude Code (`claude`) nor Codex (`codex`) is on PATH. Install one, or "
            "rerun with --executor command to drive your own agent.",
            file=sys.stderr,
        )
        return 1

    command_lines = ""
    editable: list[str] = []
    if executor == "command":
        command = ask("Command that runs one task", "")
        editable = [g.strip() for g in ask("Editable files, comma-separated globs", "").split(",")]
        editable = [g for g in editable if g]
        command_lines = COMMAND_LINES.format(command=_q(command), editable=_q(editable))
        channel = "claude" if found["claude"] else "codex"
        model, improver = DEFAULTS[channel][0], DEFAULTS[channel][2]
    else:
        model, channel, improver = DEFAULTS[executor]
    model = ask("Model that runs the tasks (provider/model)", model)
    improver = ask("Model that edits the harness (provider/model)", improver)
    harness = ask("Where the harness lives, relative to the project root", ".")

    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        CONFIG.format(
            name=_q(executor),
            model=_q(model),
            command_lines=command_lines,
            channel=_q(channel),
            improver=_q(improver),
            harness=_q(harness),
        ),
        encoding="utf-8",
    )
    ignore = config.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text("runs/\n.env\n", encoding="utf-8")
    (config.parent / "tasks").mkdir(exist_ok=True)

    print()
    print(_harness_summary(root / harness, executor, editable))
    print(_key_summary(root, (model, improver)))
    print(f"\nWrote {config.relative_to(root)}")
    if executor == "command" and not (command_lines and editable):
        print("Fill in executor.command and executor.editable before the first run.")
    print(
        "\nNext:\n"
        "  harness2 task new <id>     describe a task the agent should get better at\n"
        "  harness2 doctor            check the agent CLIs, keys and tasks\n"
        "  harness2 run --dry-run     preview what a run will do and how many calls it makes"
    )
    return 0


def _asker(*, interactive: bool):
    def ask(question: str, default: str) -> str:
        if not interactive:
            return default
        shown = f" [{default}]" if default else ""
        try:
            answer = input(f"{question}{shown}: ").strip()
        except EOFError:
            answer = ""
        return answer or default

    return ask


def _version(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        return ""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return "installed"
    return (out.stdout or out.stderr).strip().splitlines()[0] if out.returncode == 0 else ""


def _pick_executor(ask, found: dict[str, str], default: str | None) -> str | None:
    options = ["claude", "codex", "command"]
    if default is None:
        return None
    print("Which agent should run your tasks?")
    for i, name in enumerate(options, 1):
        detail = found.get(name) or ("not found" if name != "command" else "your own command")
        print(f"  {i}) {name:<8} {detail}")
    while True:
        answer = ask("Choose", str(options.index(default) + 1))
        if answer in options:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        print(f"Type a number from 1 to {len(options)}.")


def _harness_summary(where: Path, executor: str, editable: list[str]) -> str:
    """One line on what the harness holds today, so a wrong path shows up immediately."""
    if executor == "command":
        found = sorted({p for g in editable for p in where.glob(g) if p.is_file()})
        return f"Harness: {len(found)} editable file(s) under {where}."
    from ..substrate.claude import ClaudeSubstrate
    from ..substrate.codex import CodexSubstrate

    sub = ClaudeSubstrate() if executor == "claude" else CodexSubstrate()
    tree = sub.import_tree(where)
    surface = sub.surface()
    always = surface.always_on[0]
    parts = [always if tree.get(always, "").strip() else f"no {always} yet"]
    if skills := HT.skill_names(surface, tree):
        parts.append(f"{len(skills)} skill(s)")
    if agents := HT.subagent_names(surface, tree):
        parts.append(f"{len(agents)} subagent(s)")
    if surface.hooks_file in tree:
        parts.append("hooks")
    return f"Harness found in {where}: {', '.join(parts)}."


def _key_summary(root: Path, models: tuple[str, ...]) -> str:
    """Whether the API key each chosen model needs is visible to harness2."""
    from .. import providers

    S.load_dotenv(root / ".harness2/.env")
    loaded = S.load(None)
    lines = []
    for name in dict.fromkeys(providers.split_model(m)[0] for m in models if "/" in m):
        try:
            env = loaded.provider(name).api_key_env
        except S.ConfigError:
            lines.append(f"Provider {name}: not configured; add [providers.{name}] to the config.")
            continue
        if env and not os.environ.get(env):
            lines.append(
                f"{env} is not set: export it, or put {env}=... in .harness2/.env (ignored by git)."
            )
        elif env:
            lines.append(f"{env} is set.")
    return "\n".join(lines)


def _q(value) -> str:
    return S._literal(value)
