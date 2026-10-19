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

"""Render cached scores for one or more runs."""

from __future__ import annotations

import json
from pathlib import Path

from ..core import report as R
from ..core import schedule as SCHED
from ..settings import ConfigError
from . import _common as C
from .judge import _load_rows


def _spend_and_fallbacks(ns_dir: Path, modes: tuple[str, ...], k: int) -> tuple[dict, dict]:
    """(fell_back, usd) keyed (mode token, k), from the summaries the sweep checkpointed."""
    fell_back: dict[tuple[str, int], int] = {}
    usd: dict[tuple[str, int], float] = {}
    for summary_path in sorted(ns_dir.glob("*/summary.json")):
        try:
            summary = json.loads(summary_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read {summary_path}: {exc}") from exc
        records = SCHED._modes_of(summary)
        for mode in modes:
            key = (mode, k)
            rec = (records or {}).get(mode) or {}
            fell_back[key] = fell_back.get(key, 0) + len(rec.get("fell_back") or [])
            spend = sum(float(s.get("usd") or 0.0) for s in rec.get("stages") or [])
            sel = (summary.get("selections") or {}).get(mode) or {}
            usd[key] = usd.get(key, 0.0) + spend + float(sel.get("usd") or 0.0)
    return fell_back, usd


def add_parser(subparsers):
    p = subparsers.add_parser(
        "report",
        help="summarise a run's grades",
        description="Average the grades `harness2 judge` wrote, per recursion mode. Name several "
        "runs to compare them.",
    )
    p.add_argument("names", nargs="*", help="run names (default: the most recent run)")
    return p


def run(args, ctx) -> int:
    reports, rows = [], []
    for name in args.names or [None]:
        root = C.resolve_run(ctx, name)
        record = C.load_run(root)
        scored, fallbacks, spend = [], {}, {}
        for path in sorted((root / "adapt").glob("*/judged.json")):
            got = _load_rows(path)
            scored.extend(got)
            for k in {row.k for row in got}:
                fb, usd = _spend_and_fallbacks(
                    path.parent, tuple({row.mode for row in got if row.k == k}), k
                )
                for key, value in fb.items():
                    fallbacks[key] = fallbacks.get(key, 0) + value
                for key, value in usd.items():
                    spend[key] = spend.get(key, 0) + value
        rows.extend(
            {"name": root.name, **row.__dict__, "frac": row.frac, "graded": row.graded}
            for row in scored
        )
        reports.append(
            R.render(
                R.summarise(scored, fell_back=fallbacks, usd=spend),
                title=root.name,
                bench=record["suite"],
            )
        )
    if not rows:
        raise ConfigError("nothing to report")
    ctx.out(rows, lambda _: "\n\n".join(reports))
    return 0
