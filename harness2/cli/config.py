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

"""Inspect and edit layered settings."""

from __future__ import annotations

from .. import settings


def add_parser(subparsers):
    p = subparsers.add_parser("config", help="read and change settings")
    commands = p.add_subparsers(dest="action", required=True)
    show = commands.add_parser("show", help="print the resolved settings")
    show.add_argument(
        "--origin", action="store_true", help="also print where each value comes from"
    )
    get = commands.add_parser("get", help="print one setting")
    get.add_argument("key", help="dotted key, e.g. executor.model")
    set_parser = commands.add_parser("set", help="change a setting in the project config file")
    set_parser.add_argument("key", help="dotted key, e.g. run.workers")
    set_parser.add_argument("value", help="new value, read as a TOML literal when it parses")
    set_parser.add_argument(
        "--global",
        dest="global_",
        action="store_true",
        help="change ~/.config/harness2/config.toml instead",
    )
    commands.add_parser("path", help="print where the config files are")
    return p


def run(args, ctx) -> int:
    config = ctx.settings
    if args.action == "show":
        values = config.flat()
        if args.origin:
            values = {
                key: {"value": value, "origin": config.origin(key)} for key, value in values.items()
            }
        ctx.out(values, _show)
    elif args.action == "get":
        value = config.get(args.key)
        ctx.out(value, str)
    elif args.action == "set":
        path = settings.global_path() if args.global_ else settings.project_path(ctx.project)
        value = settings.parse_value(args.value)
        settings.load(
            ctx.project_or_none,
            profile=ctx.args.profile,
            overrides={**ctx.overrides, args.key: (value, "config set")},
        )
        settings.set_value(path, args.key, value)
        ctx.out(
            {"path": path, "key": args.key, "value": value},
            lambda data: f"Set {data['key']} = {settings._literal(data['value'])} in {path}",
        )
    else:
        ctx.out(
            {
                "global": settings.global_path(),
                "project": settings.project_path(ctx.project_or_none)
                if ctx.project_or_none
                else None,
            },
            lambda data: "\n".join(f"{key}: {value}" for key, value in data.items()),
        )
    return 0


def _show(values: dict) -> str:
    """`key = value` lines in TOML syntax; with --origin, where each value comes from."""
    lines = []
    for key, entry in sorted(values.items()):
        value, origin = (
            (entry["value"], entry["origin"]) if isinstance(entry, dict) else (entry, "")
        )
        literal = "(unset)" if value is None else settings._literal(value)
        lines.append(f"{key} = {literal}" + (f"    # {origin}" if origin else ""))
    return "\n".join(lines)
