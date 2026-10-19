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

"""Write a harness a run adopted back into the project, after showing what will change.

Only what the run changed relative to the harness it started from is applied, so edits made
to the project since then are kept. A file the run changed that has also changed in the
project is skipped unless --force. Only the executor's harness files are touched (for
Claude Code: CLAUDE.md, .claude/skills, .claude/agents, .claude/scripts and the hooks in
.claude/settings.json), and they must have no uncommitted changes, so git can show and undo
the result.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys

from . import _adopt as A


def add_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "apply",
        help="write the final harness into the project",
        description="Show what a run's final harness changes, then write those changes into "
        "the project. NAME defaults to the most recent run; --task takes one task's adopted "
        "harness instead.",
    )
    p.add_argument("name", nargs="?", help="run name (default: the most recent run)")
    p.add_argument("--task", help="take the harness adopted for this task")
    p.add_argument(
        "--mode",
        choices=("parallel", "sequential"),
        default="parallel",
        help="take the harness adopted in this recursion mode",
    )
    p.add_argument(
        "-i", "--interactive", action="store_true", help="review and accept each file separately"
    )
    p.add_argument("-y", "--yes", action="store_true", help="apply without asking")
    p.add_argument("--branch", help="create and switch to this git branch first")
    p.add_argument("--dry-run", action="store_true", help="show the changes, write nothing")
    p.add_argument(
        "--force",
        action="store_true",
        help="also overwrite files changed in the project since the run started, and apply "
        "over uncommitted changes",
    )
    return p


def run(args, ctx) -> int:
    if not ctx.args.json:
        return _apply(args, ctx)[0]
    if not (args.yes or args.dry_run):
        message = "--json cannot ask for confirmation; add --yes or --dry-run"
        print(json.dumps({"error": message, "exit_code": 1}))
        return 1
    with contextlib.redirect_stdout(sys.stderr):  # the review text; stdout carries the JSON
        code, summary = _apply(args, ctx)
    print(json.dumps(summary))
    return code


def _apply(args, ctx) -> tuple[int, dict]:
    adapter, run_dir = A.open_run(ctx, args.name)
    task, adopted = A.harness_for(adapter, run_dir, args.task, args.mode)
    base = A.base_tree(adapter)
    current = A.project_tree(adapter, ctx.settings)
    where = ctx.settings.path("paths.harness")

    delta = A.changes(base, adopted)
    if not delta:
        ctx.log(f"The harness adopted for {task} is the one the run started from; nothing to do.")
        return 0, {"run": run_dir.name, "harness": task, "changes": []}
    moved = {rel for rel, _ in delta if current.get(rel) != base.get(rel)}
    todo = [(rel, kind) for rel, kind in delta if args.force or rel not in moved]

    label = (
        f"Final harness of run {run_dir.name} ({args.mode})"
        if args.task is None and task == "consolidated"
        else f"Harness adopted for {task} (run {run_dir.name}, {args.mode})"
    )
    print(f"{label} -> {where}\n")
    for rel, kind in delta:
        mark = "!" if rel in moved else kind
        print(f"  {mark} {rel:<48} {_delta(rel, current, adopted)}")
    if moved:
        print(
            "\n  ! also changed in the project since the run started: "
            + ("will be overwritten (--force)." if args.force else "skipped; --force overwrites.")
        )
    print()

    def summary(applied=(), done=None) -> dict:
        return {
            "run": run_dir.name,
            "harness": task,
            "mode": args.mode,
            "changes": [
                {
                    "path": rel,
                    "kind": kind,
                    "changed_in_project": rel in moved,
                    "applied": (rel, kind) in applied,
                }
                for rel, kind in delta
            ],
            "written": (done or {}).get("written", []),
            "deleted": (done or {}).get("deleted", []),
        }

    if not todo:
        ctx.log("Nothing left to apply.")
        return 0, summary()

    dirty = A.dirty_paths(ctx.settings, adapter)
    if dirty and not args.force:
        message = (
            "These harness files have uncommitted changes:\n  "
            + "\n  ".join(dirty)
            + "\nCommit or stash them first so the result can be reviewed and undone with git, "
            "or pass --force."
        )
        print(message, file=sys.stderr)
        return 1, {"error": message, "exit_code": 1}
    if args.dry_run:
        return 0, summary()

    if args.interactive and not sys.stdin.isatty():
        message = "--interactive needs a terminal; use --yes or --dry-run instead."
        print(message, file=sys.stderr)
        return 1, {"error": message, "exit_code": 1}
    if args.interactive:
        chosen = _review(todo, current, adopted)
    elif args.yes:
        chosen = todo
    elif not sys.stdin.isatty():
        message = "Not a terminal: pass --yes to apply, or --dry-run to only look."
        print(message, file=sys.stderr)
        return 1, {"error": message, "exit_code": 1}
    else:
        chosen = todo if _confirm(f"Apply {len(todo)} change(s)?") else []
    if not chosen:
        ctx.log("Nothing applied.")
        return 0, summary()

    final = dict(current)
    for rel, kind in chosen:
        if kind == "D":
            final.pop(rel, None)
        else:
            final[rel] = adopted[rel]
    done = A.perform(ctx.settings, adapter, final, base=current, branch=args.branch)
    print(f"Wrote {len(done.get('written', []))} file(s), deleted {len(done.get('deleted', []))}.")
    if args.branch:
        print(f"On branch {args.branch}.")
    print("Review with `git diff`; undo with `git restore` on the files listed above.")
    return 0, summary(chosen, done)


def _delta(rel: str, before: dict, after: dict) -> str:
    body = A.unified(rel, before, after).splitlines()[2:]  # skip the ---/+++ header
    added = sum(line.startswith("+") for line in body)
    removed = sum(line.startswith("-") for line in body)
    return " ".join(
        s for s in (f"+{added}" if added else "", f"-{removed}" if removed else "") if s
    )


def _review(changes, current, adopted):
    chosen = []
    for i, (rel, kind) in enumerate(changes, 1):
        print(A.unified(rel, current, adopted) or f"(no difference in {rel})")
        answer = _ask(f"[{i}/{len(changes)}] Apply {rel}? [y,n,q]", ["y", "n", "q"])
        if answer == "q":
            break
        if answer == "y":
            chosen.append((rel, kind))
    return chosen


def _confirm(question: str) -> bool:
    return _ask(f"{question} [y/N]", ["y", "n", ""]) == "y"


def _ask(prompt: str, allowed: list[str]) -> str:
    while True:
        try:
            answer = input(prompt + " ").strip().lower()
        except EOFError:
            return "q" if "q" in allowed else "n"
        if answer in allowed:
            return answer
