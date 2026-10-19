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

"""The harness2 command dispatcher."""

from __future__ import annotations

import argparse
import importlib
import sys
from importlib.metadata import version

from ._common import Context

COMMANDS = (
    "init",
    "doctor",
    "task",
    "run",
    "status",
    "stop",
    "judge",
    "report",
    "runs",
    "diff",
    "eval",
    "apply",
    "config",
    "chat",
    "guide",
    "login",
    "setup",
    "demo",
    "bridge",
    "completion",
)
ALIASES = {"harness2-" + name: name for name in ("judge", "report", "setup", "demo", "bridge")}
ALIASES["harness2-check"] = "doctor"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness2",
        description="Tune your coding agent's harness on your own tasks.",
        epilog="Start with `harness2 init` in your project. "
        "Documentation: https://github.com/3ae3ae/harness2-cli",
    )
    parser.add_argument("-C", "--project", metavar="DIR", help="run as if started in DIR")
    parser.add_argument("--profile", metavar="NAME", help="apply the settings profile NAME")
    parser.add_argument(
        "-c",
        "--config",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a setting, e.g. -c run.k=2 (repeatable)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="print results only")
    parser.add_argument("-v", "--verbose", action="store_true", help="print debug details")
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    parser.add_argument("--version", action="version", version=version("harness2-cli"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        module = importlib.import_module(f"harness2.cli.{name}")
        module.add_parser(subparsers)
        subparsers.choices[name].set_defaults(_module=module)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    alias = ALIASES.get(sys.argv[0].rsplit("/", 1)[-1])
    if alias:
        # Aliases still accept global options before their subcommand's arguments.
        index = 0
        while index < len(argv):
            flag = argv[index].split("=", 1)[0]
            if flag in ("-C", "--project", "--profile", "-c", "--config"):
                index += 1 if "=" in argv[index] else 2
            elif argv[index].startswith(("-C", "-c")):
                index += 1
            elif flag in ("-q", "--quiet", "-v", "--verbose", "--json", "--version"):
                index += 1
            else:
                break
        argv.insert(index, alias)
    args = build_parser().parse_args(argv)
    args.argv = argv
    ctx = Context(args, getattr(args._module, "FLAGS", {}))
    try:
        ctx.debug(f"command: {args.command}")
        return args._module.run(args, ctx)
    except KeyboardInterrupt:
        if args.json:
            ctx.out({"error": "interrupted", "exit_code": 130}, str)
        return 130
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        if args.json:
            ctx.out({"error": str(exc), "exit_code": 1}, str)
        else:
            ctx.log(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
