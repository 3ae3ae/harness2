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

"""Manage task inputs for the built-in suite."""

from __future__ import annotations

from pathlib import Path


def add_parser(subparsers):
    p = subparsers.add_parser("task")
    commands = p.add_subparsers(dest="action", required=True)
    new = commands.add_parser("new")
    new.add_argument("id")
    new.add_argument("--files", action="store_true", help="Create a files workspace.")
    new.add_argument("--domain", default="default", help="Task domain.")
    new.add_argument("--check", help="Judge command.")
    commands.add_parser("ls")
    import_parser = commands.add_parser("import")
    import_parser.add_argument("file", type=Path)
    import_parser.add_argument("--force", action="store_true", help="Replace existing tasks.")
    return p


def run(args, ctx) -> int:
    ctx.project
    from .. import suite

    tasks_dir = ctx.settings.path("paths.tasks")
    if args.action == "new":
        result = suite.new_task(
            tasks_dir, args.id, files=args.files, domain=args.domain, check=args.check
        )
        ctx.out({"path": result}, lambda data: str(data["path"]))
    elif args.action == "ls":
        ctx.out(
            suite.list_tasks(tasks_dir),
            lambda rows: "\n".join(
                f"{row['id']}\t{row['domain']}\t{row['split']}\t{row['workspace']}\t{row['has_judge']}"
                for row in rows
            ),
        )
    else:
        ctx.out(
            suite.import_tasks(tasks_dir, args.file, force=args.force), lambda rows: "\n".join(rows)
        )
    return 0
