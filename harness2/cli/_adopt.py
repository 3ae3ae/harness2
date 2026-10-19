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

"""Open stored runs and export selected harness trees."""

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path

from .. import registry
from ..core import schedule
from ..core.spec import BenchmarkAdapter
from ..core.tree import Tree, read_tree
from ..settings import ConfigError
from . import _common as C


def open_run(ctx, name: str | None) -> tuple[BenchmarkAdapter, Path]:
    root = C.resolve_run(ctx, name)
    record = C.load_run(root)
    config = C.run_settings(ctx, record)
    split_file = record["schedule"].get("split_file")
    suite_ctx = registry.SuiteContext(
        config, ctx.project, root, Path(split_file) if split_file else None
    )
    return registry.load(record["suite"]).make_adapter(suite_ctx), root


def adopted_tree(adapter, run_dir, task: str | None, mode: str) -> tuple[str, Tree]:
    record = C.load_run(run_dir)
    mode = schedule.mode_token(mode)
    plan = record["schedule"]
    ns = schedule.ns(
        adapter.spec.name, adapter.substrate.name, mode, plan["k"], seed=plan.get("seed")
    )
    summary_ns = C.summary_namespace(adapter, plan["k"], seed=plan.get("seed"))
    tasks = [task] if task else [pair[1] for pair in reversed(record["stream"])]
    rows = {
        row["task_id"]: row
        for path, row in C.summaries(run_dir)
        if path.parent.parent.name == summary_ns
    }
    for task_id in tasks:
        selection = (rows.get(task_id, {}).get("selections") or {}).get(mode) or {}
        if not selection.get("ok") or not selection.get("chosen_label"):
            continue
        if selection.get("chosen_tree_is_base"):
            return task_id, base_tree(adapter)
        path = (
            C.runs_root_for(adapter)
            / ns
            / task_id.replace("/", "__")
            / "harnesses"
            / selection["chosen_label"]
        )
        if not path.is_dir():
            raise ConfigError(f"selected harness is missing: {path}")
        return task_id, read_tree(adapter.substrate.surface(), path)
    raise ConfigError(f"no successful selection for {task or 'any task'} in {C.mode_label(mode)}")


def base_tree(adapter) -> Tree:
    return read_tree(adapter.substrate.surface(), adapter.base_harness())


def harness_for(adapter, run_dir, task: str | None, mode: str) -> tuple[str, Tree]:
    if task is None:
        path = run_dir / "consolidated" / C.mode_label(schedule.mode_token(mode)) / "harness"
        if path.is_dir():
            return "consolidated", read_tree(adapter.substrate.surface(), path)
    return adopted_tree(adapter, run_dir, task, mode)


def project_tree(adapter, settings) -> Tree:
    return adapter.substrate.import_tree(settings.path("paths.harness"))


def changes(before: Tree, after: Tree) -> list[tuple[str, str]]:
    return [
        (rel, "A" if rel not in before else "D" if rel not in after else "M")
        for rel in sorted(before.keys() | after.keys())
        if before.get(rel) != after.get(rel)
    ]


def unified(rel: str, before: Tree, after: Tree) -> str:
    lines = []
    for line in difflib.unified_diff(
        before.get(rel, "").splitlines(keepends=True),
        after.get(rel, "").splitlines(keepends=True),
        fromfile=f"a/{rel}" if rel in before else "/dev/null",
        tofile=f"b/{rel}" if rel in after else "/dev/null",
    ):
        lines.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(lines)


def dirty_paths(settings, adapter) -> list[str]:
    root = settings.path("paths.harness")
    probe = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    if probe.returncode:
        return []
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
            "--",
            *adapter.substrate.NATIVE_PATHS,
        ],
        capture_output=True,
        check=True,
    )
    fields = result.stdout.decode("utf-8", errors="replace").split("\0")
    paths, index = set(), 0
    while index < len(fields) and fields[index]:
        entry = fields[index]
        paths.add(entry[3:])
        index += 1
        if "R" in entry[:2] or "C" in entry[:2]:
            paths.add(fields[index])
            index += 1
    return sorted(paths)


def perform(settings, adapter, tree: Tree, *, base: Tree, branch: str | None) -> dict:
    root = settings.path("paths.harness")
    if branch:
        result = subprocess.run(
            ["git", "-C", str(root), "switch", "-c", branch], capture_output=True, text=True
        )
        if result.returncode:
            raise ConfigError(result.stderr.strip() or f"cannot create branch {branch!r}")
    return adapter.substrate.export_tree(tree, root, base=base)
