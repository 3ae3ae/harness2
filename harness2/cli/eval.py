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

"""Compare consolidated and starting harnesses on a run's tasks."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from ..core import events
from ..settings import ConfigError
from . import _adopt as A
from . import _common as C
from .run import _csv, _stopped


def add_parser(subparsers):
    p = subparsers.add_parser(
        "eval",
        help="compare the final harness with the starting one",
        description="Run the agent with the starting harness and with the final one on "
        "the run's tasks, grade both with each task's check or rubric, and compare. Costs one "
        "agent run per task, harness and repeat.",
    )
    p.add_argument("name", nargs="?", help="run name (default: the most recent run)")
    p.add_argument(
        "--mode",
        choices=("parallel", "sequential"),
        default="parallel",
        help="compare the harness consolidated in this recursion mode",
    )
    p.add_argument(
        "--split", choices=("all", "train", "test"), default="all", help="compare one split only"
    )
    p.add_argument("--only-tasks", default="", metavar="ID,...", help="compare only these tasks")
    p.add_argument("--repeat", type=int, default=1, metavar="N", help="runs per task and harness")
    return p


def _rollout(adapter, task, harness, run_id):
    events.check()
    run_dir = adapter.execute(task, harness, run_id)
    cost = getattr(adapter, "rollout_cost", None)
    events.emit(
        "rollout",
        task=task,
        run_id=run_id,
        cost_usd=float(cost(run_dir)) if callable(cost) else 0.0,
    )
    try:
        grade = adapter.score(run_dir, task)
    except events.Stopped:
        raise
    except Exception:
        return None
    if not grade:
        return None
    passed, total = grade.get("n_passed"), grade.get("n_criteria")
    if type(passed) is not int or type(total) is not int or total <= 0 or not 0 <= passed <= total:
        return None
    return {"n_passed": passed, "n_criteria": total}


def _mean(values):
    graded = [value for value in values if value is not None]
    return sum(graded) / len(graded) if graded else None


def _text(data):
    def percent(value):
        return "-" if value is None else f"{value * 100:g}%"

    start, consolidated = data["mean"]["start"], data["mean"]["consolidated"]
    delta = (
        f"{(consolidated - start) * 100:+g}"
        if start is not None and consolidated is not None
        else "-"
    )
    rows = [("TASK", "START", "FINAL")]
    rows += [
        (task, percent(row["start"]["mean"]), percent(row["consolidated"]["mean"]))
        for task, row in data["tasks"].items()
    ]
    rows.append(("mean", percent(start), percent(consolidated)))
    width = max(len(r[0]) for r in rows)
    table = [f"  {a:<{width}}  {b:>5}  {c:>5}" for a, b, c in rows]
    table[-1] += f"  ({delta} points)"
    lines = [
        f"Comparing the final harness with the starting one on {len(data['tasks'])} "
        f"task(s), {data['repeat']} run(s) each.",
        "",
        *table,
        "",
    ]
    if data["skipped"]:
        lines.append(
            f"Skipped {len(data['skipped'])} task(s) with no check or rubric: "
            + ", ".join(data["skipped"])
        )
    lines.append(f"Saved to {data['path']}")
    return "\n".join(lines)


def run(args, ctx) -> int:
    if args.repeat < 1:
        raise ConfigError("--repeat must be a positive integer")
    with ctx.progress():
        adapter, root = A.open_run(ctx, args.name)
    harness = root / "consolidated" / args.mode / "harness"
    if not harness.is_dir():
        raise ConfigError("this run has no final harness yet; run `harness2 run` to finish it")
    record = C.load_run(root)
    allowed = set(adapter.tasks(args.split))
    wanted = set(_csv(args.only_tasks))
    tasks = [
        task for _, task in record["stream"] if task in allowed and (not wanted or task in wanted)
    ]
    missing = wanted - set(tasks)
    if missing:
        raise ConfigError(
            f"--only-tasks names unknown task id(s) {sorted(missing)} for split {args.split!r}"
        )
    has_judge = getattr(adapter, "has_judge", lambda task: True)
    skipped = [task for task in tasks if not has_judge(task)]
    tasks = [task for task in tasks if task not in skipped]
    config = ctx.settings
    events.start(
        root / "events.jsonl",
        budget_usd=config.get("run.budget_usd"),
        max_calls=config.get("run.max_calls"),
    )
    rows = {task: {name: {"runs": []} for name in ("start", "consolidated")} for task in tasks}
    jobs = [
        (task, name, directory, f"eval/{name}/{task}/{i}")
        for task in tasks
        for i in range(args.repeat)
        for name, directory in (("start", None), ("consolidated", harness))
    ]
    try:
        with ctx.progress(), ThreadPoolExecutor(max_workers=config.get("run.phase_width")) as pool:
            futures = [
                pool.submit(_rollout, adapter, task, directory, run_id)
                for task, _, directory, run_id in jobs
            ]
            for (task, name, _, _), future in zip(jobs, futures):
                rows[task][name]["runs"].append(future.result())
    except events.Stopped as exc:
        reason = str(exc)
        ctx.out(events.totals(), lambda totals: _stopped(root.name, reason, totals))
        return 4
    for row in rows.values():
        for result in row.values():
            result["mean"] = _mean(
                [
                    grade["n_passed"] / grade["n_criteria"] if grade else None
                    for grade in result["runs"]
                ]
            )
    path = root / "eval" / args.mode / "results.json"
    data = {
        "tasks": rows,
        "mean": {
            name: _mean([row[name]["mean"] for row in rows.values()])
            for name in ("start", "consolidated")
        },
        "repeat": args.repeat,
        "skipped": skipped,
        "path": str(path),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    ctx.out(data, _text)
    return (
        3
        if any(
            grade is None
            for row in rows.values()
            for result in row.values()
            for grade in result["runs"]
        )
        else 0
    )
