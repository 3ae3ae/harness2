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

"""Configure and execute a resumable task stream."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import core, registry
from ..core import events, ledger, schedule
from ..core.proposer import Proposer
from ..settings import ConfigError
from . import _common as C

FLAGS = {
    "suite": "suite.name",
    "executor": "executor.name",
    "k": "run.k",
    "modes": "run.modes",
    "seed": "run.seed",
    "workers": "run.workers",
    "budget_usd": "run.budget_usd",
    "max_calls": "run.max_calls",
    "selector": "selector.channel",
    "steer": "improver.steer",
}


def add_parser(subparsers):
    p = subparsers.add_parser("run")
    p.add_argument("--suite", help="Suite name.")
    p.add_argument("--bench", dest="suite", help=argparse.SUPPRESS)
    p.add_argument("--executor", help="Executor name.")
    p.add_argument("--name", help="Run name.")
    p.add_argument("--k", type=int, help="Recursion scale.")
    p.add_argument("--mode", "--modes", dest="modes", help="Comma-separated recursion modes.")
    p.add_argument("--seed", type=int, help="Task ordering seed.")
    p.add_argument("--split", choices=("all", "train", "test"), default="all", help="Task split.")
    p.add_argument("--split-file", type=Path, help="Task split file.")
    p.add_argument("--only-domains", default="", help="Comma-separated domains.")
    p.add_argument("--only-tasks", default="", help="Comma-separated task IDs.")
    p.add_argument("--max-tasks", type=int, help="Tasks per domain.")
    p.add_argument("--workers", type=int, help="Concurrent domains.")
    p.add_argument("--budget-usd", type=float, help="Spending limit in USD.")
    p.add_argument("--max-calls", type=int, help="Model call limit.")
    p.add_argument("--selector", choices=("human",), help="Interactive selector.")
    p.add_argument("--steer", metavar="TEXT|@FILE", help="Improver guidance.")
    p.add_argument("--dry-run", action="store_true", help="Preview tasks and call estimates.")
    return p


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in raw.split(",") if s.strip())


def _estimate(tasks: int, k: int, modes: tuple[str, ...], totals: dict) -> dict:
    per_mode = {
        "rollouts": ledger.expected_rollouts(k),
        "improver": ledger.expected_sessions(k),
        "selector": int(k > 1),
        "reflector": 1,
    }
    # Reference rollouts are shared by modes; reflection also runs at k=1.
    rollouts = tasks * (k + (ledger.expected_rollouts(k) - k) * len(modes))
    sessions = (
        tasks * len(modes) * sum(per_mode[key] for key in ("improver", "selector", "reflector"))
    )
    return {
        "per_task_per_mode": per_mode,
        "rollouts": rollouts,
        "sessions": sessions,
        "calls": rollouts + sessions,
        "average_cost_per_call": totals["spent_usd"] / totals["calls"] if totals["calls"] else None,
    }


def _print_stream(data) -> str:
    lines = [
        f"# {data['suite']}-{data['substrate']} split={data['split']} "
        f"seed={data['seed']} k={data['k']} modes={','.join(data['modes'])}"
    ]
    lines.extend(f"#   namespace[{mode}]: {ns}" for mode, ns in data["namespaces"].items())
    lines.extend(
        (
            f"#   summaries under: {data['summaries']}",
            f"#   library root: {data['library']}",
            f"#   domains parallel: {data['workers']}",
        )
    )
    lines.extend(
        f"{i:>4}  {domain:<40} {task}" for i, (domain, task) in enumerate(data["stream"], 1)
    )
    lines.append("# domains of this split, with task counts (the legal --only-domains names):")
    lines.extend(f"{len(tasks):>4}  {domain}" for domain, tasks in data["domains"].items())
    cap = f", max {data['max_tasks']} per domain" if data["max_tasks"] is not None else ""
    lines.append(
        f"# {len(data['stream'])} tasks over {len(data['domains'])} domains "
        f"(domains parallel, tasks within a domain sequential{cap})"
    )
    lines.append(f"# call estimate: {data['estimate']}")
    return "\n".join(lines)


def run(args, ctx) -> int:
    events.start(None)
    status, reason = "error", ""
    try:
        project, config = ctx.project, ctx.settings
        k, seed = config.get("run.k"), config.get("run.seed")
        modes = C.parse_modes(config.get("run.modes"))
        name = args.name or C.default_name(config)
        root = C.run_path(ctx, name)
        suite = registry.load(config.get("suite.name"))
        split_file = (project / args.split_file).resolve() if args.split_file else None
        suite_ctx = registry.SuiteContext(config, project, root, split_file)
        with ctx.progress():
            adapter = suite.make_adapter(suite_ctx)
            problems = suite.check(suite_ctx) if hasattr(suite, "check") else []
        if problems:
            raise ConfigError("\n".join(problems))
        filters = dict(
            split=args.split,
            seed=seed,
            only_domains=_csv(args.only_domains),
            only_tasks=_csv(args.only_tasks),
            max_tasks=args.max_tasks,
        )
        domains = schedule.task_stream(adapter, **filters)
        stream = [(domain, task) for domain, tasks in domains.items() for task in tasks]
        if not stream:
            raise ConfigError("zero tasks scheduled")
        if args.dry_run:
            ctx.out(
                {
                    "name": name,
                    "suite": adapter.spec.name,
                    "substrate": adapter.substrate.name,
                    "split": args.split,
                    "seed": seed,
                    "max_tasks": args.max_tasks,
                    "k": k,
                    "modes": [C.mode_label(m) for m in modes],
                    "namespaces": {
                        C.mode_label(m): schedule.ns(
                            adapter.spec.name, adapter.substrate.name, m, k, seed=seed
                        )
                        for m in modes
                    },
                    "summaries": C.runs_root_for(adapter)
                    / C.summary_namespace(adapter, k, seed=seed),
                    "library": C.library_root_for(adapter, k, seed=seed),
                    "workers": config.get("run.workers"),
                    "domains": domains,
                    "stream": stream,
                    "estimate": _estimate(len(stream), k, modes, C.event_totals(root)),
                },
                _print_stream,
            )
            status = "ok"
            return 0
        C.save_run(
            root,
            config,
            stream,
            {
                "k": k,
                "modes": config.get("run.modes"),
                **filters,
                "split_file": str(split_file) if split_file else None,
            },
        )
        core.configure(
            **{
                key: config.get(f"run.{key}")
                for key in ("phase_width", "transport_attempts", "proposer_concurrency")
            }
        )
        events.start(
            root / "events.jsonl",
            budget_usd=config.get("run.budget_usd"),
            max_calls=config.get("run.max_calls"),
        )
        audit = root / "adapt/_audit" / C.summary_namespace(adapter, k, seed=seed)
        prop = Proposer(adapter.spec.channels["proposer"], audit_dir=audit)
        selector_spec = adapter.spec.channels["selector"]
        if selector_spec.kind == "human":
            from .human import HumanSelector

            selector = HumanSelector(selector_spec, audit_dir=audit)
        else:
            selector = Proposer(selector_spec, audit_dir=audit)
        reflector = Proposer(
            adapter.spec.channels.get(
                "reflector",
                adapter.spec.channels["proposer"]
                if selector_spec.kind == "human"
                else selector_spec,
            ),
            audit_dir=audit,
        )
        events.emit("run_start", name=name)
        with ctx.progress():
            result = schedule.run_sweep(
                k,
                adapter=adapter,
                prop=prop,
                selector_prop=selector,
                reflector_prop=reflector,
                runs_root=C.runs_root_for(adapter),
                library_root=C.library_root_for(adapter, k, seed=seed),
                modes=modes,
                domain_parallel=config.get("run.workers"),
                **filters,
            )
        failed = sum(int(row.get("failed", 0)) for row in result["domains"])
        errors = [row["error"] for row in result["domains"] if row.get("error")]
        status, reason = (
            ("error", "; ".join(errors))
            if errors
            else (("failed", "failed tasks") if failed else ("ok", ""))
        )
        ctx.out(
            {"result": result, "totals": events.totals()},
            lambda data: f"{name}: {status}; {data['totals']}",
        )
        return 1 if errors else 3 if failed else 0
    except events.Stopped as exc:
        reason = str(exc)
        status = "interrupted" if reason == "interrupted" else "stopped"
        ctx.out(events.totals(), str)
        if status == "interrupted":
            ctx.log("run the same command to resume")
        return 130 if status == "interrupted" else 4
    except KeyboardInterrupt:
        status, reason = "interrupted", "interrupted"
        events.request_stop(reason)
        ctx.out(events.totals(), str)
        ctx.log("run the same command to resume")
        return 130
    except Exception as exc:
        reason = str(exc)
        raise
    finally:
        events.emit("run_end", status=status, reason=reason)
