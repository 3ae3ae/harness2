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

from datetime import datetime, timezone

from . import _common as C


def add_parser(subparsers):
    p = subparsers.add_parser("runs")
    commands = p.add_subparsers(dest="action", required=True)
    commands.add_parser("ls")
    show = commands.add_parser("show")
    show.add_argument("name")
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
        ctx.out(describe(C.resolve_run(ctx, args.name)), str)
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
        ctx.out(
            rows,
            lambda data: "\n".join(
                f"{row['name']}\t{row['suite']}\t{row['executor']}\t{row['done']}/{row['total']}\t${row['spent_usd']:.6f}\t{row['last_activity']}"
                for row in data
            ),
        )
    return 0
