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
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import core, registry
from ..core import events, ledger, schedule
from ..core.proposer import Proposer
from ..settings import ConfigError
from . import _common as C
from ._final import consolidate

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
    p = subparsers.add_parser(
        "run",
        help="refine the harness on the tasks",
        description="Run the agent on every task, propose and probe harness edits, and adopt a "
        "refined harness per task. Running the same command again resumes and reuses every "
        "finished step. Model calls cost money: preview with --dry-run, cap with --budget-usd "
        "or --max-calls.",
    )
    p.add_argument("--suite", help="task suite (default: tasks, your own)")
    p.add_argument("--bench", dest="suite", help=argparse.SUPPRESS)
    p.add_argument("--executor", help="agent that runs the tasks: claude, codex or command")
    p.add_argument("--name", help="run name (default: <executor>-k<k>)")
    p.add_argument("--k", type=int, help="recursion budget per task (default 1)")
    p.add_argument(
        "--mode",
        "--modes",
        dest="modes",
        help="parallel (default), sequential, or both separated by a comma",
    )
    p.add_argument("--seed", type=int, help="shuffle the task order within each domain")
    p.add_argument(
        "--split", choices=("all", "train", "test"), default="all", help="run one split only"
    )
    p.add_argument("--split-file", type=Path, help="task partition file (benchmark suites)")
    p.add_argument("--only-domains", default="", metavar="D,...", help="run only these domains")
    p.add_argument("--only-tasks", default="", metavar="ID,...", help="run only these tasks")
    p.add_argument("--max-tasks", type=int, metavar="N", help="run at most N tasks per domain")
    p.add_argument("--workers", type=int, metavar="N", help="domains to run at the same time")
    p.add_argument(
        "--budget-usd", type=float, metavar="USD", help="stop once the run has spent this much"
    )
    p.add_argument("--max-calls", type=int, metavar="N", help="stop after N model calls")
    p.add_argument(
        "--selector", choices=("human",), help="pick the final harness yourself (when k > 1)"
    )
    p.add_argument("--steer", metavar="TEXT|@FILE", help="guidance added to every improver session")
    p.add_argument(
        "--dry-run", action="store_true", help="show the tasks and the model calls; run nothing"
    )
    p.add_argument(
        "--detach", action="store_true", help="start the run in the background and return at once"
    )
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
    ) + len(modes)
    return {
        "per_task_per_mode": per_mode,
        "rollouts": rollouts,
        "sessions": sessions,
        "calls": rollouts + sessions,
        "average_cost_per_call": totals["spent_usd"] / totals["calls"] if totals["calls"] else None,
    }


def _spend(totals: dict) -> str:
    # Codex and subscriptions report no money; $0.00 would read as "free".
    money = f"${totals['spent_usd']:.2f} spent" if totals["spent_usd"] else "spend not reported"
    return f" · {money} · {totals['calls']} model calls"


def _stopped(name: str, reason: str, totals: dict) -> str:
    why = {
        "interrupted": "interrupted",
        "budget_usd": f"reached --budget-usd {totals['budget_usd']:g}",
        "max_calls": f"reached --max-calls {totals['max_calls']}",
        "usage_limit": "reached the model provider's usage limit",
    }.get(reason, reason)
    advice = (
        "Run the same command to resume."
        if reason == "interrupted"
        else "Raise the limit and run the same command to resume."
    )
    if reason == "usage_limit":
        advice = "Run the same command once the limit resets to resume."
    return f"Run {name} stopped: {why}{_spend(totals)}.\n{advice}"


def _print_stream(data) -> str:
    domains, stream, est = data["domains"], data["stream"], data["estimate"]
    lanes = data["workers"]
    lines = [
        f"Run {data['name']} would refine {len(stream)} task(s) in {len(domains)} domain(s) "
        f"(k={data['k']}, {', '.join(data['modes'])}), "
        + ("one domain at a time." if lanes == 1 else f"{lanes} domains at a time."),
        "",
    ]
    width = max(6, *(len(d) for d in domains))
    lines.append(f"  {'#':>4}  {'DOMAIN':<{width}}  TASK")
    lines.extend(
        f"  {i:>4}  {domain:<{width}}  {task}" for i, (domain, task) in enumerate(stream, 1)
    )
    lines += [
        "",
        f"Model calls: {est['rollouts']} agent runs + {est['sessions']} improver, selector and "
        f"reflector sessions = {est['calls']} (retries can add more).",
    ]
    if est["average_cost_per_call"] is not None:
        cost = est["average_cost_per_call"] * est["calls"]
        lines.append(
            f"At this run's average so far, ${est['average_cost_per_call']:.3f} per call, that "
            f"is about ${cost:.2f} in total."
        )
    lines.append(f"Stored in {data['run_dir']}")
    return "\n".join(lines)


def _detach(args, ctx, name: str, root: Path) -> int:
    root.mkdir(parents=True, exist_ok=True)
    log = root / "run.log"
    argv = [arg for arg in args.argv if arg != "--detach"]
    if args.name is None:
        argv.extend(["--name", name])
    with log.open("a", encoding="utf-8") as output:
        child = subprocess.Popen(
            [sys.executable, "-m", "harness2", *argv],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        code = child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        code = None
    if code:
        print(
            "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]),
            file=sys.stderr,
        )
        return code
    ctx.out(
        {"name": name, "pid": child.pid, "log": log},
        lambda data: (
            f"Started run {name} in the background (pid {data['pid']}).\n"
            f"Follow it: harness2 status {name} --watch\n"
            f"Stop it:   harness2 stop {name}\n"
            f"Log:       {log}"
        ),
    )
    return 0


def run(args, ctx) -> int:
    events.start(None)
    status, reason, name = "error", "", args.name or ""
    pid_path = None
    try:
        if args.detach and args.dry_run:
            raise ConfigError("--detach starts a real run; drop --dry-run")
        project, config = ctx.project, ctx.settings
        k, seed = config.get("run.k"), config.get("run.seed")
        modes = C.parse_modes(config.get("run.modes"))
        name = args.name or C.default_name(config)
        root = C.run_path(ctx, name)
        if not args.dry_run and (process := C.run_process(root)) is not None:
            raise ConfigError(f"run {name} is already running (pid {process['pid']})")
        if args.detach:
            return _detach(args, ctx, name, root)
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
                    "run_dir": root,
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
        pid_path = root / "run.pid"
        pid_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "started": datetime.now(timezone.utc).isoformat(),
                    "argv": args.argv,
                }
            )
            + "\n",
            encoding="utf-8",
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
        done = sum(int(row.get("done", 0)) for row in result["domains"])
        final = {}
        for mode in modes:
            label = C.mode_label(mode)
            try:
                if not any(
                    path.parent.parent.name == audit.name
                    and (selection := (row.get("selections") or {}).get(mode) or {}).get("ok")
                    and selection.get("chosen_label")
                    for path, row in C.summaries(root)
                ):
                    continue
                final[label] = consolidate(ctx, adapter, root, label)["path"]
            except Exception as exc:
                print(
                    f"Could not build the final harness ({label}): {exc}. "
                    "Run the same command again to retry.",
                    file=sys.stderr,
                )
        if errors and args.json:
            raise ConfigError(reason)
        ctx.out(
            {"result": result, "totals": events.totals(), "final": final},
            lambda data: (
                f"Run {name}: {done} task(s) done, {failed} failed"
                + _spend(data["totals"])
                + (f"\nErrors: {reason}" if errors else "")
                + "".join(f"\nFinal harness ({mode}): {path}" for mode, path in final.items())
                + f"\nNext: harness2 diff {name} shows it; harness2 apply {name} writes it into "
                f"the project; harness2 eval {name} measures it first."
            ),
        )
        return 1 if errors else 3 if failed else 0
    except events.Stopped as exc:
        reason = str(exc)
        status = "interrupted" if reason == "interrupted" else "stopped"
        ctx.out(
            {"error": "interrupted", "exit_code": 130}
            if status == "interrupted" and args.json
            else events.totals(),
            lambda totals: _stopped(name, reason, totals),
        )
        return 130 if status == "interrupted" else 4
    except KeyboardInterrupt:
        status, reason = "interrupted", "interrupted"
        events.request_stop(reason)
        ctx.out(
            {"error": "interrupted", "exit_code": 130} if args.json else events.totals(),
            lambda totals: _stopped(name, reason, totals),
        )
        return 130
    except Exception as exc:
        reason = str(exc)
        raise
    finally:
        try:
            events.emit("run_end", status=status, reason=reason)
        finally:
            if pid_path is not None:
                pid_path.unlink(missing_ok=True)
