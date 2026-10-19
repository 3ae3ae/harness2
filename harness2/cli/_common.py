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

"""CLI settings, output, and persisted run conventions."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property
from importlib.metadata import version
from pathlib import Path

from .. import settings
from ..core import library as LIB
from ..core import schedule
from ..settings import ConfigError


@dataclass
class Context:
    args: object
    flags: dict[str, str]

    @cached_property
    def project_or_none(self) -> Path | None:
        return settings.find_project(Path(self.args.project or Path.cwd()))

    @property
    def project(self) -> Path:
        if self.project_or_none is None:
            raise ConfigError(
                "not inside a harness2 project (no .harness2/ found); run `harness2 init`"
            )
        return self.project_or_none

    @cached_property
    def overrides(self) -> dict:
        values = {}
        for raw in self.args.config:
            key, sep, value = raw.partition("=")
            if not sep or not key.strip():
                raise ConfigError("--config requires KEY=VALUE")
            values[key.strip()] = (settings.parse_value(value), "-c")
        for name, key in self.flags.items():
            value = getattr(self.args, name, None)
            if value is not None:
                values[key] = (value, f"flag --{name.replace('_', '-')}")
        return values

    @cached_property
    def settings(self) -> settings.Settings:
        return settings.load(
            self.project_or_none, profile=self.args.profile, overrides=self.overrides
        )

    def out(self, data, text_fn) -> None:
        if self.args.json:
            print(json.dumps(data, ensure_ascii=False, default=str))
        else:
            text = text_fn(data)
            if text is not None:
                print(text)

    def log(self, message: str) -> None:
        if not self.args.quiet:
            print(message, file=sys.stderr)

    def debug(self, message: str) -> None:
        if self.args.verbose:
            self.log(message)

    @contextmanager
    def progress(self):
        if self.args.quiet:
            with open(os.devnull, "w") as stream, redirect_stdout(stream):
                yield
        else:
            with redirect_stdout(sys.stderr):
                yield


def run_path(ctx: Context, name: str) -> Path:
    if not name or name in (".", "..") or Path(name).name != name:
        raise ConfigError("run name must be a single directory name")
    return ctx.settings.path("paths.runs") / name


def resolve_run(ctx: Context, name: str | None) -> Path:
    ctx.project
    if name is not None:
        path = run_path(ctx, name)
        if not (path / "run.json").is_file():
            raise ConfigError(f"run {name!r} has no run.json")
        return path
    root = ctx.settings.path("paths.runs")
    runs = (
        [p for p in root.iterdir() if p.is_dir() and (p / "run.json").is_file()]
        if root.is_dir()
        else []
    )
    if not runs:
        raise ConfigError(f"no runs under {root}")
    return max(runs, key=lambda p: p.stat().st_mtime_ns)


def run_process(run_dir: Path) -> dict | None:
    path = run_dir / "run.pid"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(record, dict)
            or type(record.get("pid")) is not int
            or record["pid"] <= 0
            or not isinstance(record.get("started"), str)
            or not isinstance(record.get("argv"), list)
            or any(not isinstance(arg, str) for arg in record["argv"])
        ):
            raise ValueError("invalid run process record")
    except FileNotFoundError:
        return None
    except ValueError:
        path.unlink(missing_ok=True)
        return None
    try:
        os.kill(record["pid"], 0)
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        return None
    except PermissionError:
        pass
    return record


def load_run(run_dir: Path) -> dict:
    try:
        record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        if not isinstance(record, dict) or not all(
            key in record for key in ("name", "settings", "suite", "executor", "stream", "schedule")
        ):
            raise ValueError("missing run metadata")
        if not isinstance(record["settings"], dict) or not isinstance(record["schedule"], dict):
            raise ValueError("settings and schedule must be objects")
        if not isinstance(record["stream"], list) or any(
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(item, str) for item in pair)
            for pair in record["stream"]
        ):
            raise ValueError("stream must contain [domain, task] pairs")
        return record
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {run_dir / 'run.json'}: {exc}") from exc


def identity(config: settings.Settings) -> dict:
    return {
        key: value
        for key, value in config.flat().items()
        if key.startswith(("suite.", "executor.", "improver.", "selector.", "reflector."))
        or key in ("paths.harness", "paths.tasks", "run.seed", "run.obs_trunc")
    }


def default_name(config: settings.Settings) -> str:
    name = f"{config.get('executor.name')}-k{config.get('run.k')}"
    seed = config.get("run.seed")
    return name + (f"-s{seed}" if seed is not None else "")


def save_run(run_dir: Path, config: settings.Settings, stream: list, plan: dict) -> dict:
    subset = identity(config)
    path = run_dir / "run.json"
    old = load_run(run_dir) if path.is_file() else None
    if old:
        differences = [
            f"{key}: {old['settings'].get(key)!r} -> {subset.get(key)!r}"
            for key in sorted(old["settings"].keys() | subset.keys())
            if old["settings"].get(key) != subset.get(key)
            or (key in old["settings"]) != (key in subset)
        ]
        if old["stream"] != [list(pair) for pair in stream]:
            differences.append(f"stream: {old['stream']!r} -> {stream!r}")
        if differences:
            raise ConfigError("run identity differs:\n" + "\n".join(differences))
    record = {
        "name": run_dir.name,
        "created": old["created"] if old else datetime.now(timezone.utc).isoformat(),
        "harness2_version": version("harness2-cli"),
        "suite": config.get("suite.name"),
        "executor": config.get("executor.name"),
        "settings": subset,
        "stream": [list(pair) for pair in stream],
        "schedule": plan,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return record


def run_settings(ctx: Context, record: dict) -> settings.Settings:
    overrides = {key: (value, "run.json") for key, value in record["settings"].items()}
    for key in ("k", "modes", "seed"):
        if key in record["schedule"]:
            overrides[f"run.{key}"] = (record["schedule"][key], "run.json")
    overrides.update(
        {key: value for key, value in ctx.overrides.items() if key.startswith("judge.")}
    )
    current = settings.load(ctx.project, profile=ctx.args.profile, overrides=overrides)
    data = {name: current.section(name) for name in settings.DEFAULTS}
    origins = {key: current.origin(key) for key in current.flat()}
    # Empty snapshot sections must not inherit fields added to the project since the run.
    for name in ("suite", "executor", "improver", "selector", "reflector"):
        data[name] = {}
    for key, value in record["settings"].items():
        parts = key.split(".")
        table = data
        for part in parts[:-1]:
            table = table.setdefault(part, {})
        table[parts[-1]] = value
        origins[key] = "run.json"
    return settings.Settings(ctx.project, data, origins)


def runs_root_for(adapter) -> Path:
    return adapter.spec.runs_root / "adapt"


def library_root_for(adapter, k: int, *, seed: int | None, tag: str = "") -> Path:
    schedule.validate_tag(tag)
    variant = adapter.substrate.name
    if seed is not None:
        variant += f"-s{seed}"
    if tag:
        variant += f"-{tag}"
    return LIB.root_for(
        adapter.spec.runs_root / "library", benchmark=adapter.spec.name, k=k, variant=variant
    )


def summary_namespace(adapter, k: int, *, seed: int | None, tag: str = "") -> str:
    return schedule.ns(adapter.spec.name, adapter.substrate.name, "agg", k, seed=seed, tag=tag)


def parse_modes(raw: str) -> tuple[str, ...]:
    tokens = {schedule.mode_token(s.strip()) for s in raw.split(",") if s.strip()}
    if not tokens:
        raise ConfigError("--mode requires parallel or sequential")
    return tuple(t for t in schedule.MODES if t in tokens)


def mode_label(token: str) -> str:
    return schedule.MODE_NAMES.get(token, token)


def summaries(run_dir: Path) -> list[tuple[Path, dict]]:
    rows = []
    for path in sorted((run_dir / "adapt").glob("*/*/summary.json")):
        try:
            rows.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
    return rows


def event_totals(run_dir: Path) -> dict:
    records = []
    path = run_dir / "events.jsonl"
    if path.is_file():
        try:
            records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
    calls = [
        r
        for r in records
        if r["kind"] == "rollout" or (r["kind"] == "session" and not r.get("cached"))
    ]
    return {
        "spent_usd": round(sum(r.get("cost_usd", 0) or 0 for r in records), 6),
        "calls": len(calls),
        "last_activity": records[-1].get("t") if records else None,
    }
