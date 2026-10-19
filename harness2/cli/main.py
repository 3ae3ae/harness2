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
    "run",
    "doctor",
    "setup",
    "judge",
    "report",
    "runs",
    "diff",
    "task",
    "config",
    "demo",
    "bridge",
    "completion",
    "init",
    "status",
    "chat",
    "apply",
)
ALIASES = {"harness2-" + name: name for name in ("judge", "report", "setup", "demo", "bridge")}
ALIASES["harness2-check"] = "doctor"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness2")
    parser.add_argument("-C", "--project", metavar="DIR", help="Project directory.")
    parser.add_argument("--profile", metavar="NAME", help="Settings profile.")
    parser.add_argument(
        "-c",
        "--config",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override a setting.",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Silence logs.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug logs.")
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    parser.add_argument("--version", action="version", version=version("harness2"))
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
    ctx = Context(args, getattr(args._module, "FLAGS", {}))
    try:
        ctx.debug(f"command: {args.command}")
        return args._module.run(args, ctx)
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        ctx.log(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
