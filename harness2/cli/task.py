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
    p = subparsers.add_parser("task", help="create, list and import tasks")
    commands = p.add_subparsers(dest="action", required=True)
    new = commands.add_parser("new", help="create a task directory")
    new.add_argument("id", help="task id: lowercase letters, digits, '.', '_' and '-'")
    new.add_argument(
        "--files",
        action="store_true",
        help="a files task: inputs in files/, results in output/ (default: a project task)",
    )
    new.add_argument("--domain", default="default", help="domain the task belongs to")
    new.add_argument("--check", help="shell command that grades a result")
    commands.add_parser("ls", help="list tasks")
    import_parser = commands.add_parser("import", help="create tasks from a JSONL or CSV file")
    import_parser.add_argument("file", type=Path, help="the .jsonl or .csv file")
    import_parser.add_argument(
        "--force", action="store_true", help="overwrite tasks that already exist"
    )
    return p


def run(args, ctx) -> int:
    ctx.project
    from .. import suite

    tasks_dir = ctx.settings.path("paths.tasks")
    if args.action == "new":
        result = suite.new_task(
            tasks_dir, args.id, files=args.files, domain=args.domain, check=args.check
        )
        ctx.out(
            {"path": result},
            lambda data: (
                f"Created {data['path']}. Write the request in its task.md"
                + (" and put the input files in files/." if args.files else ".")
            ),
        )
    elif args.action == "ls":
        ctx.out(
            suite.list_tasks(tasks_dir),
            _table,
        )
    else:
        ctx.out(
            suite.import_tasks(tasks_dir, args.file, force=args.force), lambda rows: "\n".join(rows)
        )
    return 0


def _table(rows: list[dict]) -> str:
    if not rows:
        return "No tasks yet; create one with `harness2 task new <id>`."
    head = ("ID", "DOMAIN", "SPLIT", "WORKSPACE", "GRADED")
    body = [
        (r["id"], r["domain"], r["split"] or "-", r["workspace"], "yes" if r["has_judge"] else "no")
        for r in rows
    ]
    widths = [max(len(str(row[i])) for row in (head, *body)) for i in range(len(head))]
    return "\n".join(
        "  ".join(str(cell).ljust(w) for cell, w in zip(row, widths)).rstrip()
        for row in (head, *body)
    )
