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

"""List persisted runs and task progress."""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone

from . import _common as C
from . import status as S


def add_parser(subparsers):
    p = subparsers.add_parser("runs", help="list runs or show one")
    commands = p.add_subparsers(dest="action", required=True)
    commands.add_parser("ls", help="list runs with their progress and spend")
    show = commands.add_parser("show", help="show a run's settings, totals and tasks")
    show.add_argument("name", help="run name")
    rm = commands.add_parser("rm", help="delete a run and everything it stored")
    rm.add_argument("name", help="run name")
    rm.add_argument("-y", "--yes", action="store_true", help="delete without asking")
    return p


def describe(root) -> dict:
    record = C.load_run(root)
    plan = record["schedule"]
    summaries = {
        row["task_id"]: row
        for _, row in C.summaries(root)
        if row.get("k") == plan["k"] and row.get("seed") == plan.get("seed")
    }
    statuses = [
        {"task": task, "domain": domain, "status": summaries.get(task, {}).get("status", "pending")}
        for domain, task in record["stream"]
    ]
    totals = C.event_totals(root)
    return {
        "run": record,
        "totals": totals,
        "tasks": statuses,
        "done": sum(row["status"] in ("ok", "skipped") for row in statuses),
        "total": len(statuses),
        "last_activity": totals["last_activity"]
        or datetime.fromtimestamp(root.stat().st_mtime, timezone.utc).isoformat(),
    }


def run(args, ctx) -> int:
    if args.action == "show":
        root = C.resolve_run(ctx, args.name)
        ctx.out(describe(root), lambda data: _show(root, data))
    elif args.action == "rm":
        return _rm(ctx, C.resolve_run(ctx, args.name), args.yes)
    else:
        ctx.project
        root = ctx.settings.path("paths.runs")
        paths = (
            sorted(
                (p for p in root.iterdir() if (p / "run.json").is_file()),
                key=lambda p: p.stat().st_mtime_ns,
                reverse=True,
            )
            if root.is_dir()
            else []
        )
        rows = []
        for path in paths:
            detail = describe(path)
            rows.append(
                {
                    "name": path.name,
                    "suite": detail["run"]["suite"],
                    "executor": detail["run"]["executor"],
                    "done": detail["done"],
                    "total": detail["total"],
                    "spent_usd": detail["totals"]["spent_usd"],
                    "last_activity": detail["last_activity"],
                }
            )
        ctx.out(rows, _table)
    return 0


def _table(rows: list[dict]) -> str:
    if not rows:
        return "No runs yet; start one with `harness2 run`."
    head = ("NAME", "SUITE", "EXECUTOR", "TASKS", "SPENT", "LAST ACTIVITY")
    body = [
        (
            r["name"],
            r["suite"],
            r["executor"],
            f"{r['done']}/{r['total']}",
            f"${r['spent_usd']:.2f}",
            S._when(r["last_activity"]),
        )
        for r in rows
    ]
    widths = [max(len(row[i]) for row in (head, *body)) for i in range(len(head))]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() for row in (head, *body)
    )


def _show(root, data: dict) -> str:
    settings = data["run"]["settings"]
    keys = ("executor.name", "executor.model", "improver.channel", "improver.model")
    lines = [S.render(S.snapshot(root)), "", "Settings:"]
    lines += [f"  {key} = {settings[key]}" for key in keys if key in settings]
    return "\n".join(lines)


def _rm(ctx, root, yes: bool) -> int:
    size = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    if not yes:
        if not sys.stdin.isatty():
            print("Not a terminal: pass --yes to delete.", file=sys.stderr)
            return 1
        question = f"Delete run {root.name} and everything it stored ({size / 1e6:.1f} MB)? [y/N] "
        if input(question).strip().lower() != "y":
            ctx.log("Nothing deleted.")
            return 0
    shutil.rmtree(root)
    ctx.log(f"Deleted run {root.name}.")
    return 0
