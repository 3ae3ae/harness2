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

"""Run the real recursion and library workflow with local, deterministic editors."""

from __future__ import annotations

import json
from pathlib import Path

from ..core.schedule import mode_token, run_domain
from ..demo import DemoAdapter, DemoProposer


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "demo",
        help="run the method locally, with scripted editors and no model calls",
        description="Run the real recursion and library workflow on two toy tasks, with "
        "deterministic editors instead of models. Takes a few seconds and costs nothing.",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("runs/demo"), help="where to write the demo run"
    )
    parser.add_argument("--k", type=int, default=2, help="recursion budget per task")
    parser.add_argument(
        "--mode",
        choices=("parallel", "sequential", "both"),
        default="both",
        help="recursion mode",
    )
    return parser


def run(args, ctx) -> int:
    if args.k < 1:
        raise ValueError("--k must be positive")
    root = args.output.expanduser().resolve()
    adapter = DemoAdapter(root)
    proposer = DemoProposer(adapter.spec.channels["proposer"])
    modes = ("agg", "seq") if args.mode == "both" else (mode_token(args.mode),)
    with ctx.progress():
        result = run_domain(
            "accounting",
            adapter.tasks(),
            args.k,
            adapter=adapter,
            prop=proposer,
            runs_root=adapter.spec.runs_root,
            library_root=root / "library" / f"k{args.k}",
            modes=modes,
        )
    result["execution_count"] = adapter.execution_count
    result["editor_calls"] = len(proposer._calls)
    report = root / "demo.json"
    report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    ctx.out(
        result,
        lambda data: (
            f"Local demo: {data['done']} tasks completed; {data['failed']} failed. {report}"
        ),
    )
    return int(result["failed"] > 0)


def main(argv=None) -> int:
    import sys

    from .main import main as cli_main

    return cli_main(["demo", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
