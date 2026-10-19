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

"""Show a run's progress from its files on disk; `--watch` keeps the view refreshed.

Reads run.json and events.jsonl only, so it works while the run is going, after it stopped,
and from another terminal.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import _common as C

_MODE = {"agg": "parallel", "seq": "sequential"}


def add_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "status",
        help="show a run's progress",
        description="Show each task's state, the harness picked for it, and the run's spend. "
        "NAME defaults to the most recent run.",
    )
    p.add_argument("name", nargs="?", help="run name (default: the most recent run)")
    p.add_argument(
        "--watch",
        nargs="?",
        type=float,
        const=5.0,
        metavar="SECONDS",
        help="redraw every SECONDS (default 5) until Ctrl-C",
    )
    return p


def run(args, ctx) -> int:
    run_dir = C.resolve_run(ctx, args.name)
    if not args.watch:
        snap = snapshot(run_dir)
        ctx.out(snap, render)
        return 0
    try:
        while True:
            text = render(snapshot(run_dir))
            sys.stdout.write("\033[H\033[2J" + text + "\n")
            sys.stdout.flush()
            time.sleep(max(args.watch, 0.5))
    except KeyboardInterrupt:
        return 0


def snapshot(run_dir: Path) -> dict:
    """Everything the view shows, as data (this is also the --json output)."""
    meta = C.load_run(run_dir)
    events = _events(run_dir / "events.jsonl")
    tasks: dict[str, dict] = {}
    for domain, task in meta.get("stream", []):
        tasks[task] = {
            "task": task,
            "area": domain,
            "state": "pending",
            "rollouts": 0,
            "picked": {},
        }
    spent = calls = 0.0
    ended = None
    for ev in events:
        kind, row = ev.get("kind"), tasks.get(ev.get("task", ""))
        spent += float(ev.get("cost_usd") or 0)
        if kind in ("session", "rollout") and not ev.get("cached"):
            calls += 1
        if kind == "run_start":
            ended = None
        elif kind == "run_end":
            ended = ev
        if row is None:
            continue
        if kind == "task_start":
            row["state"] = "running"
        elif kind == "task_done":
            row["state"] = "done" if ev.get("status") in ("ok", "skipped") else "failed"
        elif kind == "rollout":
            row["rollouts"] += 1
        elif kind == "selection" and ev.get("chosen"):
            row["picked"][_MODE.get(ev.get("mode"), ev.get("mode"))] = ev["chosen"]
    if ended is not None:
        for row in tasks.values():
            if row["state"] == "running":
                row["state"] = "stopped"
    plan = meta.get("schedule", {})
    process = C.run_process(run_dir)
    return {
        "active": process is not None,
        "pid": process["pid"] if process else None,
        "run": meta.get("name", run_dir.name),
        "suite": meta.get("suite", ""),
        "executor": meta.get("executor", ""),
        "k": plan.get("k"),
        "modes": plan.get("modes", ""),
        "created": meta.get("created", ""),
        "spent_usd": round(spent, 4),
        "calls": int(calls),
        "ended": ended,
        "tasks": list(tasks.values()),
        "last_event": events[-1] if events else None,
    }


def render(snap: dict) -> str:
    rows = snap["tasks"]
    head = f"{snap['run']} · {snap['suite']} suite · {snap['executor']}"
    if snap["k"]:
        head += f" · k={snap['k']} {snap['modes']}"
    if snap.get("active"):
        head += f" · running (pid {snap['pid']})"
    last = snap["last_event"]
    lines = [
        head,
        f"started {_when(snap['created'])}"
        + (f" · last activity {_ago(last.get('t'))}" if last else "")
        + (f" · ${snap['spent_usd']:.2f} spent" if snap["spent_usd"] else " · spend not reported")
        + f" · {snap['calls']} model calls",
        "",
    ]
    if rows:
        w = max(4, *(len(r["task"]) for r in rows))
        a = max(4, *(len(r["area"]) for r in rows))
        lines.append(f"  {'TASK':<{w}}  {'AREA':<{a}}  {'STATE':<8}  {'ROLLOUTS':>8}  PICKED")
        for r in rows:
            picked = ", ".join(f"{m}: {c}" for m, c in r["picked"].items())
            lines.append(
                f"  {r['task']:<{w}}  {r['area']:<{a}}  {r['state']:<8}  "
                f"{r['rollouts'] or '':>8}  {picked}"
            )
        lines.append("")
    states = ("done", "running", "stopped", "failed", "pending")
    counts = [(sum(r["state"] == s for r in rows), s) for s in states]
    lines.append(" · ".join(f"{n} {s}" for n, s in counts if n) or "no tasks")
    end = snap["ended"]
    if end:
        status = end.get("status", "")
        why = f" ({end['reason']})" if end.get("reason") else ""
        tail = "" if status == "ok" else " · run the same command to resume"
        lines.append(f"run {'finished' if status == 'ok' else 'ended: ' + status}{why}{tail}")
    elif last:
        lines.append(f"latest: {_describe(last)}")
        if not snap.get("active"):
            lines.append(
                "not running: it stopped without finishing; run the same command to resume"
            )
    return "\n".join(lines)


def _events(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue  # a line being written right now
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _describe(ev: dict) -> str:
    kind = ev.get("kind", "")
    where = f"{ev['task']} · " if ev.get("task") else ""
    if kind == "rollout":
        what = f"rollout {ev.get('label', '')} " + (
            "delivered" if ev.get("delivered") else "produced nothing"
        )
    elif kind == "session":
        what = f"{ev.get('label', 'session')} " + ("done" if ev.get("ok") else "failed")
    elif kind == "selection":
        what = f"picked {ev.get('chosen', '?')} ({_MODE.get(ev.get('mode'), ev.get('mode'))})"
    else:
        what = kind.replace("_", " ")
    return f"{where}{what} · {_ago(ev.get('t'))}"


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _when(ts: str | None) -> str:
    t = _parse(ts)
    return t.astimezone().strftime("%Y-%m-%d %H:%M") if t else "?"


def _ago(ts: str | None) -> str:
    t = _parse(ts)
    if t is None:
        return "?"
    s = int((datetime.now(timezone.utc) - t).total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"
