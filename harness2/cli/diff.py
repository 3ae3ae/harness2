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
    p = subparsers.add_parser("diff")
    p.add_argument("name", nargs="?")
    p.add_argument("--task", help="Task ID.")
    p.add_argument(
        "--mode", choices=("parallel", "sequential"), default="parallel", help="Recursion mode."
    )
    p.add_argument("--stat", action="store_true", help="Print change counts.")
    return p


def run(args, ctx) -> int:
    with ctx.progress():
        adapter, root = _adopt.open_run(ctx, args.name)
    task, after = _adopt.adopted_tree(adapter, root, args.task, args.mode)
    before = _adopt.base_tree(adapter)
    data = (
        diff_summary(adapter.substrate.surface(), before, after)
        if args.stat
        else {
            "task": task,
            "diff": "".join(
                _adopt.unified(rel, before, after) for rel, _ in _adopt.changes(before, after)
            ),
        }
    )
    ctx.out(data, str if args.stat else lambda row: row["diff"].rstrip("\n"))
    return 0
