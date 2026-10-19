# Copyright 2026 Google LLC
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

"""Judge finished selections using the saved run settings."""

from __future__ import annotations

import json
from pathlib import Path

from ..core import judge as J
from ..core.runmark import resolve_path
from ..settings import ConfigError
from . import _common as C
from ._adopt import open_run

_DERIVED = ("frac", "graded")


_PARALLEL = 8


def _load_rows(path: Path) -> list[J.Scored]:
    """Rebuild Scored rows from a judged.json written by core.judge.write."""
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable judged cache {path}: {exc}; move it aside to rebuild")
    rows = []
    for row in raw.get("rows", []) if isinstance(raw, dict) else raw:
        row = {k: v for k, v in row.items() if k not in (*_DERIVED, "judge_model")}
        if "mode" not in row and "arm" in row:
            row["mode"] = row.pop("arm")
        rows.append(J.Scored(**row))
    return rows


def _merge(old: list[J.Scored], new: list[J.Scored]) -> list[J.Scored]:
    """Per (task, mode, k): a changed chosen run replaces; for the same run_id the graded
    row wins, so a failed re-judge never erases an existing score."""
    by_key = {(r.task_id, r.mode, r.k): r for r in old}
    for r in new:
        key = (r.task_id, r.mode, r.k)
        prev = by_key.get(key)
        if (
            prev is not None
            and prev.run_id == r.run_id
            and prev.graded
            and not r.graded
            and not r.error.startswith("selection")
        ):
            continue
        by_key[key] = r
    return [by_key[k] for k in sorted(by_key)]


def _selection_record(sel: dict) -> dict:
    """A selection record keyed by `mode`; older summaries carry `arm` instead."""
    if "mode" not in sel and "arm" in sel:
        sel = dict(sel)
        sel["mode"] = sel.pop("arm")
    return sel


def collect_selections(ns_dir: Path, modes: tuple[str, ...]) -> list[dict]:
    """Every requested mode's selection record from every finished task summary."""
    out: list[dict] = []
    for summary_path in sorted(ns_dir.glob("*/summary.json")):
        try:
            summary = json.loads(summary_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read {summary_path}: {exc}") from exc
        for mode in modes:
            sel = (summary.get("selections") or {}).get(mode)
            if sel:
                out.append(_selection_record(sel))
    return out


def add_parser(subparsers):
    p = subparsers.add_parser(
        "judge",
        help="grade the result each task adopted",
        description="Grade each task's adopted result with the task's check command or "
        "rubric.md; tasks with neither are skipped. NAME defaults to the most recent run.",
    )
    p.add_argument("name", nargs="?", help="run name (default: the most recent run)")
    p.add_argument(
        "--bases",
        action="store_true",
        help="grade the runs of the starting harness instead, for comparison",
    )
    return p


def _judge_model(path, model):
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    old = data.get("judge_model") if isinstance(data, dict) else None
    if old is not None and old != model:
        raise ConfigError(f"judge model differs: {old!r} -> {model!r}; delete {path} to re-judge")


def judge_bases(adapter, ns_dir: Path, *, log, allowed) -> list[dict]:
    rows = []
    for summary_path in sorted(ns_dir.glob("*/summary.json")):
        summary = json.loads(summary_path.read_text())
        task = summary["task_id"]
        if not allowed(task):
            continue
        for entry in summary.get("base") or []:
            if not isinstance(entry, dict) or not entry.get("run_dir"):
                log(f"{task}: reference execution has no run directory")
                continue
            run_dir = resolve_path(entry["run_dir"], adapter.spec.runs_root)
            row = J.judge_one(
                adapter,
                task,
                "base",
                summary["k"],
                entry.get("label", ""),
                entry.get("run_id", ""),
                run_dir,
                chosen_is_base=True,
            )
            rows.append(row.__dict__ | {"graded": row.graded, "frac": row.frac})
    return rows


def run(args, ctx) -> int:
    with ctx.progress():
        adapter, root = open_run(ctx, args.name)
    record = C.load_run(root)
    config = C.run_settings(ctx, record)
    model = config.role("judge")["model"]
    plan = record["schedule"]
    ns_dir = C.runs_root_for(adapter) / C.summary_namespace(
        adapter, plan["k"], seed=plan.get("seed")
    )
    path = ns_dir / ("base_judged.json" if args.bases else "judged.json")
    _judge_model(path, model)
    skipped = set()

    def allowed(task):
        if not hasattr(adapter, "has_judge") or adapter.has_judge(task):
            return True
        if task not in skipped:
            ctx.log(f"{task}: no judge; skipped")
            skipped.add(task)
        return False

    if args.bases:
        with ctx.progress():
            rows = judge_bases(adapter, ns_dir, log=ctx.log, allowed=allowed)
    else:
        selections = [
            s
            for s in collect_selections(ns_dir, C.parse_modes(plan["modes"]))
            if allowed(s["task_id"])
        ]
        with ctx.progress():
            new = J.judge_selections(adapter, selections, parallel=_PARALLEL)
        merged = _merge([row for row in _load_rows(path) if allowed(row.task_id)], new)
        rows = [row.__dict__ | {"graded": row.graded, "frac": row.frac} for row in merged]
    if not rows:
        ctx.out([], lambda _: "no tasks to judge")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"judge_model": model, "rows": rows}, indent=2) + "\n", encoding="utf-8"
    )
    ctx.out(
        rows,
        lambda data: (
            f"Graded {sum(row['graded'] for row in data)} of {len(data)} result(s); "
            f"saved to {path}. Summarise with `harness2 report`."
        ),
    )
    return 0 if all(row["graded"] for row in rows) else 1
