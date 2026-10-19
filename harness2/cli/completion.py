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

"""Generate shell completion from the argparse command tree."""

from __future__ import annotations

import argparse
import shlex


def add_parser(subparsers):
    p = subparsers.add_parser(
        "completion",
        help="print a shell completion script",
        description="Print a completion script. zsh: harness2 completion zsh > "
        '"${fpath[1]}/_harness2"   bash: harness2 completion bash > '
        "~/.local/share/bash-completion/completions/harness2",
    )
    p.add_argument("shell", choices=("bash", "zsh"), help="your shell")
    return p


def generate(parser, shell: str) -> str:
    choices, transitions = {}, []

    def visit(parser, path=""):
        words = {
            option
            for action in parser._actions
            for option in action.option_strings
            if option.startswith("--") and action.help != argparse.SUPPRESS
        }
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                words.update(action.choices)
                for name, child in action.choices.items():
                    next_path = f"{path} {name}".strip()
                    transitions.append(
                        f"      {shlex.quote(path + ':' + name)}) command_path={shlex.quote(next_path)} ;;"
                    )
                    visit(child, next_path)
        choices[path] = " ".join(sorted(words))

    visit(parser)
    start = ["#compdef harness2"] if shell == "zsh" else []
    start += [
        "_harness2_complete() {",
        "  local command_path='' word candidates",
        "  local -a previous",
    ]
    if shell == "zsh":
        start += ['  previous=("${words[@]:1:$((CURRENT-2))}")']
    else:
        start += ['  previous=("${COMP_WORDS[@]:1:$((COMP_CWORD-1))}")']
    start += [
        '  for word in "${previous[@]}"; do',
        '    case "$command_path:$word" in',
        *transitions,
        "    esac",
        "  done",
        '  case "$command_path" in',
    ]
    start += [
        f"    {shlex.quote(path)}) candidates={shlex.quote(words)} ;;"
        for path, words in choices.items()
    ]
    start += ["  esac"]
    if shell == "zsh":
        start += ["  compadd -- ${=candidates}", "}", "compdef _harness2_complete harness2"]
    else:
        start += [
            '  COMPREPLY=( $(compgen -W "$candidates" -- "${COMP_WORDS[COMP_CWORD]}") )',
            "}",
            "complete -F _harness2_complete harness2",
        ]
    return "\n".join(start)


def run(args, ctx) -> int:
    from .main import build_parser

    ctx.out(
        {"shell": args.shell, "script": generate(build_parser(), args.shell)},
        lambda data: data["script"],
    )
    return 0
