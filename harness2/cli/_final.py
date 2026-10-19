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

"""Compose a project harness from a run's successful task selections."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..core import library, prompts, recursion, schedule, tree
from ..core.proposer import Proposer
from ..settings import ConfigError
from . import _adopt as A
from . import _common as C


def _context(
    adapter, root: Path, record: dict, mode: str, dest: Path, base: tree.Tree
) -> list[str]:
    surface = adapter.substrate.surface()
    token = schedule.mode_token(mode)
    plan = record["schedule"]
    namespace = C.summary_namespace(adapter, plan["k"], seed=plan.get("seed"))
    selected = {
        row["task_id"]
        for path, row in C.summaries(root)
        if path.parent.parent.name == namespace
        and (selection := (row.get("selections") or {}).get(token) or {}).get("ok")
        and selection.get("chosen_label")
    }
    tasks, index = [], []
    tree.write_tree(surface, base, dest / "harness", stubs=True)
    lib = C.library_root_for(adapter, plan["k"], seed=plan.get("seed")) / token
    for domain, task in record["stream"]:
        if task not in selected:
            continue
        _, adopted = A.adopted_tree(adapter, root, task, mode)
        task_dir = dest / "tasks" / library.slug(task)
        tree.write_tree(surface, adopted, task_dir / "harness", stubs=False)
        (task_dir / "task.md").write_text(adapter.task_prompt(task), encoding="utf-8")
        lesson = (
            lib / library.domain_dir(domain) / "harnesses" / library.slug(task) / "EXPERIENCE.md"
        )
        if lesson.is_file():
            (task_dir / "EXPERIENCE.md").write_text(lesson.read_text(), encoding="utf-8")
        index.append(
            f"- {task} ({domain}): adopted harness "
            f"{'differs from' if adopted != base else 'same as'} the start; "
            f"lesson: {'yes' if lesson.is_file() else 'no'}"
        )
        tasks.append(task)
    if not tasks:
        raise ConfigError(f"no successful selection for any task in {mode}")
    (dest / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    return tasks


def consolidate(ctx, adapter, root: Path, mode: str) -> dict:
    mode = C.mode_label(schedule.mode_token(mode))
    with ctx.progress():
        base = A.base_tree(adapter)
    record = C.load_run(root)
    surface = adapter.substrate.surface()
    prop = Proposer(adapter.spec.channels["proposer"], audit_dir=root / "consolidated/_audit")
    cost = 0.0
    # The context path participates in the proposer's cache key.
    context = root / "consolidated/_audit" / f"context.{mode}"
    if context.exists():
        shutil.rmtree(context)
    try:
        tasks = _context(adapter, root, record, mode, context, base)
        mission = prompts.CONSOLIDATE_MISSION.replace("{n}", str(len(tasks))).replace(
            "{surface}", prompts._st(surface).surface_md
        )
        err = ""
        for _ in range(2):
            with ctx.progress():
                reply = prop.call(
                    mission
                    + (f"\n\n# YOUR PREVIOUS ATTEMPT WAS REJECTED\n\n{err}\n\n" if err else ""),
                    context,
                    f"consolidate.{mode}",
                    harvest="context/harness",
                )
                cost += reply.cost_usd
                if reply.workspace is None:
                    err = "the harness directory did not come back from the session"
                    continue
                rejected = []
                new = tree.read_tree(surface, reply.workspace, rejected=rejected)
                _, err = recursion._accept_tree(surface, adapter, new, base, "consolidate")
                if rejected:
                    err = "files outside the editable surface: " + ", ".join(sorted(rejected))
            if not err:
                break
            tree.write_tree(surface, new, context / "harness", stubs=True)
        else:
            if reply.workspace is not None:
                shutil.rmtree(reply.workspace)
            raise ConfigError(err)
    finally:
        shutil.rmtree(context, ignore_errors=True)
    notes = re.search(r"<notes>(.*?)</notes>", reply.text, re.S)
    notes = notes.group(1).strip() if notes else reply.text.strip()
    dest = root / "consolidated" / mode
    path = tree.write_tree(surface, new, dest / "harness", stubs=False)
    (dest / "notes.md").write_text(notes + "\n", encoding="utf-8")
    (dest / "consolidation.json").write_text(
        json.dumps(
            {
                "tasks": tasks,
                "model": prop.spec.model,
                "cost_usd": cost,
                "created": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"path": path, "tasks": tasks, "changes": A.changes(base, new), "notes": notes}
