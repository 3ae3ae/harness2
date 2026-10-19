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

"""Compare the stored base harness with a selection."""

from __future__ import annotations

from ..core.tree import diff_summary
from . import _adopt


def add_parser(subparsers):
    p = subparsers.add_parser(
        "diff",
        help="show what the final harness changes",
        description="Compare a run's final harness with the harness the run started from. "
        "NAME defaults to the most recent run; --task shows one task's adopted harness instead.",
    )
    p.add_argument("name", nargs="?", help="run name (default: the most recent run)")
    p.add_argument("--task", help="show the harness adopted for this task")
    p.add_argument(
        "--mode",
        choices=("parallel", "sequential"),
        default="parallel",
        help="take the harness adopted in this recursion mode",
    )
    p.add_argument("--stat", action="store_true", help="list the changed files only")
    return p


def run(args, ctx) -> int:
    with ctx.progress():
        adapter, root = _adopt.open_run(ctx, args.name)
    task, after = _adopt.harness_for(adapter, root, args.task, args.mode)
    before = _adopt.base_tree(adapter)
    changes = _adopt.changes(before, after)
    data = {
        "label": task,
        "changes": changes,
        "diffs": {rel: _adopt.unified(rel, before, after) for rel, _ in changes},
    }
    ctx.out(
        data,
        lambda row: (
            str(diff_summary(adapter.substrate.surface(), before, after))
            if args.stat
            else "".join(row["diffs"].values()).rstrip("\n")
        ),
    )
    return 0
