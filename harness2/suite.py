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

"""The built-in suite for tasks in the user's project or supplied files."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import warnings
from dataclasses import replace
from pathlib import Path

from . import providers, workspace
from .core import events, runmark, tree
from .core.library import slug
from .core.proposer import Proposer, ProposerError, _parse_json
from .core.spec import BenchPrompts, BenchSpec, ChannelSpec
from .registry import SuiteContext
from .settings import ConfigError, Settings
from .substrate._native import checked_path, cli_argv
from .substrate.claude import ClaudeSubstrate, parse_events
from .substrate.codex import CodexSubstrate
from .substrate.command import CommandSubstrate

DESCRIPTION = "Tasks"

PROJECT_PROMPTS = BenchPrompts(
    work=(
        "Software engineering in this repository: the changes, fixes and investigations a "
        "maintainer asks for. The request controls. The result must be a change a maintainer "
        "could merge: complete, consistent with the codebase's conventions, and checked by "
        "running it."
    ),
    domain_word="area",
    deliverable_word="change",
    inv_levers=(
        "- locate the code, tests and conventions that govern the request before editing; read "
        "before writing\n"
        "- find and use the project's own way to build, run and test; run it before finishing "
        "and read what it reports\n"
        "- keep the change scoped to the request; do not reformat, rename or refactor what the "
        "request did not ask for\n"
        "- when a command fails, read the error and change approach instead of repeating it"
    ),
    dom_levers=(
        "- the codebase's architecture, module boundaries and the patterns existing code follows\n"
        "- the invariants a change in this area must keep: interfaces, data formats, error "
        "handling, compatibility\n"
        "- where the tests for this area live and what a convincing test for this kind of "
        "change looks like"
    ),
    yardstick=(
        "Did the run find the code that governs the request, make the change the request asked "
        "for and nothing else, follow the codebase's conventions, and verify the change by "
        "running it before stopping?"
    ),
    selection_checks=(
        "Prefer the change that does what the request asks, completely; fits the surrounding "
        "code's conventions; keeps the behaviour the request did not ask to change; and shows "
        "it was verified. A smaller change that fully solves the request beats a larger one."
    ),
)

FILES_PROMPTS = BenchPrompts(
    work=(
        "Producing the documents, analyses or data a request asks for from the supplied files. "
        "The request and the source files control. The result must be ready to use: complete, "
        "accurate to the sources, and in exactly the files and format requested."
    ),
    domain_word="area",
    deliverable_word="deliverable",
    inv_levers=(
        "- inventory the supplied files and map each requested output to the sources that "
        "control it\n"
        "- write every requested file early enough that it exists before the budget runs out, "
        "then reopen it and check that it is complete and parses\n"
        "- use code for counting, arithmetic and format conversion instead of doing it by hand\n"
        "- when a tool fails on a file, read the error and try one bounded alternative"
    ),
    dom_levers=(
        "- what a complete deliverable of this kind contains and how its audience will use it\n"
        "- which source governs when two sources disagree, and how to state what the sources "
        "do not say\n"
        "- the checks a practitioner runs on this kind of work before handing it over"
    ),
    yardstick=(
        "Did the run read the sources that control the request, produce every requested file in "
        "usable form, keep every figure and claim traceable to a source, and check the finished "
        "files before stopping?"
    ),
    selection_checks=(
        "Prefer the deliverable that answers every part of the request, stays accurate to the "
        "supplied files, is in exactly the requested files and format, and needs no repair "
        "before use."
    ),
)

FILES_PROMPT_SUFFIX = "\n\nSave your deliverables in the output/ directory."

RUBRIC_MISSION = """\
You are grading one result of a task against a rubric.

context/task.md is the request the agent received. context/rubric.md lists the criteria.
context/deliverables/ holds the result: the files it produced or its patch, and its final
message.

Read the request, the rubric and the whole result. For each criterion, decide whether the
result meets it, judging only what the result actually contains -- not what it claims or
promises. A criterion the result does not address is not met.

Give your reasoning per criterion, then end with exactly one JSON object on the last line:
{"n_passed": <criteria met>, "n_criteria": <criteria in the rubric>, "notes": "<one sentence>"}
"""

TASK_TEMPLATE = "TODO: describe the task the way you would ask the agent to do it.\n"
# suite.check reports a task whose task.md still starts with "TODO:":
#   "task {id}: task.md still holds the template text"

BASE_DRIFT_WARNING = (
    "the project's harness has changed since this run started; the run keeps using the copy "
    "it started with in {path}"
)

_TASK_KEYS = {"domain", "split", "workspace", "ref", "deliverable", "check", "timeout"}


def _task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", task_id):
        raise ConfigError(f"invalid task id {task_id!r}; use [a-z0-9][a-z0-9._-]*")
    return task_id


def _task_config(task_id: str, values: dict, *, files: bool) -> dict:
    _task_id(task_id)
    for key in values:
        if key not in _TASK_KEYS:
            raise ConfigError(f"task {task_id}: unknown task.toml key {key!r}")
    for key, value in values.items():
        if key == "timeout":
            if type(value) is not int or value <= 0:
                raise ConfigError(f"task {task_id}: timeout must be a positive integer")
        elif not isinstance(value, str) or not value.strip():
            raise ConfigError(f"task {task_id}: {key} must be a non-empty string")
    config = {"domain": "default", "workspace": "files" if files else "project", "ref": "HEAD"}
    config.update(values)
    config.setdefault("deliverable", "diff" if config["workspace"] == "project" else "files")
    for key, choices in (
        ("split", ("train", "test")),
        ("workspace", ("project", "files")),
        ("deliverable", ("diff", "files", "message")),
    ):
        if key in config and config[key] not in choices:
            raise ConfigError(f"task {task_id}: {key} must be one of {', '.join(choices)}")
    return config


def _read_task(tasks_dir: Path, task_id: str) -> dict:
    _task_id(task_id)
    task_dir = checked_path(tasks_dir, task_id)
    try:
        checked_path(task_dir, "task.md").read_text(encoding="utf-8")
        config_path = checked_path(task_dir, "task.toml")
        values = (
            tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        )
    except (OSError, ValueError, UnicodeError) as exc:
        raise ConfigError(f"task {task_id}: cannot read task: {exc}") from exc
    return _task_config(task_id, values, files=(task_dir / "files").is_dir())


def list_tasks(tasks_dir: Path) -> list[dict]:
    if not tasks_dir.exists():
        return []
    rows = []
    for path in sorted(tasks_dir.iterdir()):
        if not path.is_dir():
            continue
        config = _read_task(tasks_dir, path.name)
        rows.append(
            {
                "id": path.name,
                "domain": config["domain"],
                "split": config.get("split"),
                "workspace": config["workspace"],
                "has_judge": bool(config.get("check") or (path / "rubric.md").is_file()),
            }
        )
    return rows


def _write_task(task_dir: Path, prompt: str, values: dict) -> None:
    prompt_path = checked_path(task_dir, "task.md")
    config_path = checked_path(task_dir, "task.toml")
    files_path = checked_path(task_dir, "files")
    task_dir.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    config_path.write_text(
        "".join(
            f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in values.items()
        ),
        encoding="utf-8",
    )
    if values.get("workspace") == "files":
        files_path.mkdir(exist_ok=True)


def new_task(tasks_dir: Path, task_id: str, *, files: bool, domain: str, check: str | None) -> Path:
    values = {"domain": domain, "workspace": "files" if files else "project"}
    if check is not None:
        values["check"] = check
    _task_config(task_id, values, files=files)
    task_dir = checked_path(tasks_dir, task_id)
    if task_dir.exists():
        raise ConfigError(f"task {task_id} already exists")
    _write_task(task_dir, TASK_TEMPLATE, values)
    return task_dir


def import_tasks(tasks_dir: Path, source: Path, *, force: bool) -> list[str]:
    try:
        with source.open(encoding="utf-8", newline="") as stream:
            if source.suffix.lower() == ".jsonl":
                records = [json.loads(line) for line in stream if line.strip()]
            elif source.suffix.lower() == ".csv":
                records = list(csv.DictReader(stream))
            else:
                raise ConfigError("task imports must use .jsonl or .csv")
    except (OSError, ValueError, UnicodeError, csv.Error) as exc:
        raise ConfigError(f"cannot import {source}: {exc}") from exc
    pending = {}
    for record in records:
        if not isinstance(record, dict):
            raise ConfigError("each imported task must be an object")
        task_id = _task_id(record.get("id"))
        prompt = record.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ConfigError(f"task {task_id}: prompt is required")
        values = {k: v for k, v in record.items() if k not in ("id", "prompt") and v != ""}
        if source.suffix.lower() == ".csv" and "timeout" in values:
            try:
                values["timeout"] = int(values["timeout"])
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"task {task_id}: timeout must be a positive integer") from exc
        _task_config(task_id, values, files=(tasks_dir / task_id / "files").is_dir())
        if task_id in pending:
            raise ConfigError(f"duplicate task id {task_id!r} in import")
        task_dir = checked_path(tasks_dir, task_id)
        if task_dir.exists() and not force:
            raise ConfigError(f"task {task_id} already exists; use --force to replace it")
        pending[task_id] = (prompt, values)
    for task_id, (prompt, values) in pending.items():
        _write_task(tasks_dir / task_id, prompt, values)
    return list(pending)


def _channel(settings: Settings, role: str) -> ChannelSpec:
    values = settings.role(role)
    kind = values["channel"]
    if kind == "human" and role != "selector":
        raise ConfigError("the human channel is only allowed for selector")
    provider = None
    if kind != "human":
        provider_name, _ = providers.split_model(values["model"])
        provider = settings.provider(provider_name)
    try:
        return ChannelSpec(
            kind=kind,
            model=values["model"],
            bin=values["bin"],
            effort=values["effort"],
            timeout=values["timeout"],
            steps=values["steps"],
            provider=provider,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


class GenericAdapter:
    """Run local task definitions through the shared adaptation method."""

    def __init__(self, ctx: SuiteContext):
        self.ctx = ctx
        self.settings = ctx.settings
        self.tasks_dir = self.settings.path("paths.tasks")
        self.executor = self.settings.section("executor")
        name = self.executor["name"]
        if name == "claude":
            self.substrate = ClaudeSubstrate(bin=self.executor["bin"])
            install_mode = "claude_root"
        elif name == "codex":
            self.substrate = CodexSubstrate(bin=self.executor["bin"])
            install_mode = "codex_root"
        elif name == "command":
            try:
                self.substrate = CommandSubstrate(
                    self.executor["command"], tuple(self.executor["editable"])
                )
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            install_mode = "command_files"
        else:
            raise ConfigError("the tasks suite runs on claude, codex or command")
        self.spec = BenchSpec(
            name="tasks",
            runs_root=ctx.run_root,
            install_mode=install_mode,
            prompts=PROJECT_PROMPTS,
            channels={
                "proposer": _channel(self.settings, "improver"),
                "selector": _channel(self.settings, "selector"),
                "reflector": _channel(self.settings, "reflector"),
            },
            domain_word="area",
            leak_globs=("rubric.md", "check/**"),
            allow_subagents=name == "claude",
            obs_trunc=self.settings.get("run.obs_trunc"),
        )
        self._warned_drift = False
        self._base_lock = threading.Lock()

    def tasks(self, split: str = "all") -> list[str]:
        if split not in ("all", "train", "test"):
            raise ConfigError(f"unknown task split {split!r}; use all, train or test")
        return [
            row["id"]
            for row in list_tasks(self.tasks_dir)
            if split == "all" or row["split"] == split
        ]

    def domain_of(self, task_id: str) -> str:
        return _read_task(self.tasks_dir, task_id)["domain"]

    def task_prompt(self, task_id: str) -> str:
        _read_task(self.tasks_dir, task_id)
        return (self.tasks_dir / task_id / "task.md").read_text(encoding="utf-8")

    def task_files(self, task_id: str) -> Path | None:
        _read_task(self.tasks_dir, task_id)
        path = checked_path(self.tasks_dir / task_id, "files")
        return path if path.is_dir() else None

    def prompts_for(self, task_id: str) -> BenchPrompts:
        config = _read_task(self.tasks_dir, task_id)
        prompts = PROJECT_PROMPTS if config["workspace"] == "project" else FILES_PROMPTS
        steer = self.settings.get("improver.steer")
        if steer.startswith("@"):
            path = self.ctx.project / Path(steer[1:]).expanduser()
            try:
                steer = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ConfigError(f"cannot read improver.steer file {path}: {exc}") from exc
        try:
            return replace(
                prompts,
                work=self.settings.get("suite.work") or prompts.work,
                deliverable_word="answer"
                if config["deliverable"] == "message"
                else prompts.deliverable_word,
                domain_guidance=steer,
            )
        except ValueError as exc:
            raise ConfigError("improver.steer / suite.work: " + str(exc)) from exc

    def base_harness(self) -> Path:
        dest = self.spec.runs_root / "base_harness"
        surface = self.substrate.surface()
        with self._base_lock:
            current = self.substrate.import_tree(self.settings.path("paths.harness"))
            if not dest.exists():
                tree.write_tree(surface, current, dest, stubs=True)
            elif (
                not self._warned_drift
                and tree.read_tree(surface, dest) != {a: "" for a in surface.always_on} | current
            ):
                warnings.warn(BASE_DRIFT_WARNING.format(path=dest), stacklevel=2)
                self._warned_drift = True
        return dest

    def _workspace(self, task_id: str, config: dict):
        if config["workspace"] == "project":
            return workspace.project_workspace(self.ctx.project, config["ref"])
        return workspace.files_workspace(self.task_files(task_id))

    def execute(
        self, task_id: str, harness_dir: Path | None, run_id: str, *, timeout: int | None = None
    ) -> Path:
        config = _read_task(self.tasks_dir, task_id)
        harness_dir = harness_dir if harness_dir is not None else self.base_harness()
        run_name = slug(run_id)
        if not run_name or run_name in (".", "..") or "\\" in run_name:
            raise ConfigError(f"invalid rollout id {run_id!r}")
        run_dir = self.spec.runs_root / "rollouts" / run_name
        if runmark.is_complete(run_dir, harness_dir=harness_dir):
            return run_dir
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)
        prompt_file = run_dir / "prompt.md"
        prompt = self.task_prompt(task_id)
        if config["workspace"] == "files":
            prompt += FILES_PROMPT_SUFFIX
        prompt_file.write_text(prompt, encoding="utf-8")
        wall = config.get("timeout") or timeout or self.executor["timeout"]
        started = time.monotonic()
        timed_out = False
        with self._workspace(task_id, config) as ws:
            self.substrate.strip_native(ws)
            if config["workspace"] == "project":
                state = ws / ".harness2"
                if state.is_symlink() or state.is_file():
                    state.unlink()
                else:
                    shutil.rmtree(state, ignore_errors=True)
            install = self.substrate.materialise(harness_dir, self.spec, {})
            try:
                self.substrate.install_into(install, ws)
            finally:
                shutil.rmtree(install.staging_dir, ignore_errors=True)
            if config["workspace"] == "project":
                workspace.commit_all(ws, "harness2: candidate harness")
                base = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ws, text=True
                ).strip()
            env = dict(os.environ)
            if self.executor["name"] != "command":
                provider_name, model = providers.split_model(self.executor["model"])
                provider = self.settings.provider(provider_name)
                if self.executor["name"] == "claude":
                    env = self.substrate.env(env, provider, run_dir / "claude-home")
                else:
                    home = None
                    if provider.type != "chatgpt-login":
                        home = run_dir / "codex-home"
                        self.substrate.write_home(home, provider, model, self.executor["effort"])
                    env = self.substrate.env(env, home, provider=provider)
                argv = self.substrate.argv(
                    prompt_file, ws, model, self.executor["effort"], provider=provider
                )
            try:
                if self.executor["name"] == "command":
                    exit_code = self.substrate.run(prompt_file, ws, run_dir, wall, env)
                else:
                    with (
                        prompt_file.open("rb") as stdin,
                        (run_dir / "stream.jsonl").open("wb") as stdout,
                        (run_dir / "stderr.log").open("wb") as stderr,
                    ):
                        exit_code = subprocess.run(
                            argv,
                            cwd=ws,
                            env=env,
                            stdin=stdin,
                            stdout=stdout,
                            stderr=stderr,
                            timeout=wall,
                        ).returncode
            except subprocess.TimeoutExpired:
                exit_code, timed_out = -1, True
            except OSError as exc:
                (run_dir / "stderr.log").write_text(str(exc), encoding="utf-8")
                exit_code = -1
            stream = run_dir / "stream.jsonl"
            failed = exit_code != 0 or any(
                event.get("type") in ("error", "turn.failed")
                or (
                    event.get("type") == "result"
                    and (event.get("is_error") or event.get("subtype") not in (None, "success"))
                )
                for event in parse_events(stream)
            )
            if failed:
                tail = "".join(
                    path.read_text(encoding="utf-8", errors="replace")[-16000:]
                    for path in (stream, run_dir / "stderr.log")
                    if path.is_file()
                )
                if providers.USAGE_LIMIT.search(tail):
                    shutil.rmtree(run_dir)
                    events.request_stop("usage_limit")
                    raise events.Stopped("usage_limit")
            if config["workspace"] == "project":
                (run_dir / "deliverable.patch").write_text(
                    workspace.diff_since(ws, base, (*self.substrate.NATIVE_PATHS, ".harness2")),
                    encoding="utf-8",
                )
            if (ws / "output").is_dir():
                shutil.copytree(ws / "output", run_dir / "output")
            (run_dir / "final.md").write_text(
                self.substrate.final_message(stream), encoding="utf-8"
            )
            meta = {
                "task_id": task_id,
                "deliverable": config["deliverable"],
                "ok": exit_code == 0 and not timed_out,
                "exit_code": exit_code,
                "wall_s": round(time.monotonic() - started, 3),
                "cost_usd": self.substrate.cost_usd(stream),
                "timed_out": timed_out,
            }
            (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        runmark.mark_complete(run_dir, run_id=run_id, harness_dir=harness_dir)
        return run_dir

    def _meta(self, run_dir: Path) -> dict:
        """The rollout's meta.json, or {} for a rollout that died before writing one."""
        try:
            return json.loads((run_dir / "meta.json").read_text())
        except (OSError, ValueError):
            return {}

    def rollout_cost(self, run_dir: Path) -> float:
        return float(self._meta(run_dir).get("cost_usd", 0.0))

    def delivered(self, run_dir: Path) -> bool:
        kind = self._meta(run_dir).get("deliverable")
        if kind is None:
            return False
        if kind == "files":
            return any(path.is_file() for path in (run_dir / "output").rglob("*"))
        path = run_dir / ("deliverable.patch" if kind == "diff" else "final.md")
        return path.is_file() and bool(path.read_text(encoding="utf-8").strip())

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        return self.substrate.read_trajectory(
            run_dir / "stream.jsonl", obs_trunc=obs_trunc, max_chars=200_000
        )

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        kind = self._meta(run_dir).get("deliverable")
        if kind is None:
            return {}
        if kind == "files":
            paths = {
                p.relative_to(run_dir / "output").as_posix(): p
                for p in sorted((run_dir / "output").rglob("*"))
                if p.is_file()
            }
        else:
            name = "deliverable.patch" if kind == "diff" else "final.md"
            paths = {name: run_dir / name}
        files = {}
        for name, path in paths.items():
            if not path.is_file():
                continue
            data = path.read_bytes()
            try:
                files[name] = data.decode("utf-8")
            except UnicodeDecodeError:
                files[name] = f"(binary file, {len(data)} bytes)"
        return files

    def deliverable_text(self, run_dir: Path) -> str:
        files = self.deliverable_files(run_dir)
        text = "\n\n".join(files.values())
        final = run_dir / "final.md"
        if "deliverable.patch" in files and final.is_file():
            text += "\n\n" + final.read_text(encoding="utf-8")
        return text

    def validate_tree(self, tree: dict[str, str]) -> None:
        pass

    def has_judge(self, task_id: str) -> bool:
        config = _read_task(self.tasks_dir, task_id)
        return bool(config.get("check") or (self.tasks_dir / task_id / "rubric.md").is_file())

    def score(self, run_dir: Path, task_id: str) -> dict | None:
        config = _read_task(self.tasks_dir, task_id)
        task_dir = self.tasks_dir / task_id
        if config.get("check"):
            check_dir = task_dir / "check"
            digest = hashlib.sha256()
            for path in sorted(check_dir.rglob("*")):
                digest.update(path.relative_to(check_dir).as_posix().encode() + b"\0")
                digest.update(b"d" if path.is_dir() else b"f" + path.read_bytes())
                digest.update(b"\0")
            identity = {"check": config["check"], "check_sha256": digest.hexdigest()}
        elif (task_dir / "rubric.md").is_file():
            judge_channel = _channel(self.settings, "judge")
            identity = {
                "channel": judge_channel.kind,
                "model": judge_channel.model,
                "rubric_sha256": hashlib.sha256((task_dir / "rubric.md").read_bytes()).hexdigest(),
            }
        else:
            return None
        cache = run_dir / "judge.json"
        if cache.is_file():
            try:
                cached = json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = None
            if isinstance(cached, dict) and cached.get("identity") == identity:
                return cached
            cache.unlink(missing_ok=True)
        result = None
        if config.get("check"):
            with self._workspace(task_id, config) as ws:
                if config["workspace"] == "project":
                    try:
                        workspace.apply_patch(ws, (run_dir / "deliverable.patch").read_text())
                    except (OSError, subprocess.CalledProcessError):
                        return None
                elif (run_dir / "output").is_dir():
                    shutil.copytree(run_dir / "output", ws / "output", dirs_exist_ok=True)
                if (task_dir / "check").is_dir():
                    shutil.copytree(task_dir / "check", ws / "check", dirs_exist_ok=True)
                try:
                    process = subprocess.run(
                        config["check"],
                        shell=True,
                        cwd=ws,
                        text=True,
                        capture_output=True,
                        timeout=config.get("timeout") or self.executor["timeout"],
                        env=os.environ
                        | {"HARNESS2_RUN_DIR": str(run_dir), "HARNESS2_TASK_DIR": str(task_dir)},
                    )
                    code, output, stderr = process.returncode, process.stdout, process.stderr
                except subprocess.TimeoutExpired as exc:
                    code = -1
                    output = (exc.stdout or b"").decode("utf-8", errors="replace")
                    stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
                lines = output.strip().splitlines()
                try:
                    counts = _judge_counts(json.loads(lines[-1])) if lines else None
                except ValueError:
                    counts = None
                result = counts or {"n_passed": int(code == 0), "n_criteria": 1}
                result.update(exit_code=code, output_tail=(output + stderr)[-2000:])
        elif (task_dir / "rubric.md").is_file():
            with tempfile.TemporaryDirectory(prefix="harness2-judge-") as tmp:
                context = Path(tmp)
                shutil.copyfile(task_dir / "task.md", context / "task.md")
                shutil.copyfile(task_dir / "rubric.md", context / "rubric.md")
                dest = context / "deliverables"
                dest.mkdir()
                if config["deliverable"] == "files" and (run_dir / "output").is_dir():
                    shutil.copytree(run_dir / "output", dest, dirs_exist_ok=True)
                else:
                    name = "deliverable.patch" if config["deliverable"] == "diff" else "final.md"
                    if (run_dir / name).is_file():
                        shutil.copyfile(run_dir / name, dest / name)
                shutil.copyfile(run_dir / "final.md", dest / "final.md")
                try:
                    reply = Proposer(judge_channel).call(
                        mission=RUBRIC_MISSION,
                        context_dir=context,
                        label=f"judge.{task_id}.{slug(run_dir.name)}",
                    )
                except ProposerError:
                    return None
                blocks = re.findall(r"```json\s*(.*?)```", reply.text, flags=re.S)
                result = _judge_counts(_parse_json(blocks[-1] if blocks else reply.text))
        if result is not None:
            result["all_pass"] = result["n_passed"] == result["n_criteria"]
            result["identity"] = identity
            cache.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result


def _judge_counts(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    passed, total = value.get("n_passed"), value.get("n_criteria")
    if type(passed) is not int or type(total) is not int or not 0 <= passed <= total or total <= 0:
        return None
    result = {"n_passed": passed, "n_criteria": total}
    if isinstance(value.get("notes"), str):
        result["notes"] = value["notes"]
    return result


def make_adapter(ctx: SuiteContext) -> GenericAdapter:
    return GenericAdapter(ctx)


def check(ctx: SuiteContext) -> list[str]:
    problems = []
    settings = ctx.settings
    executor = settings.get("executor.name")
    if executor not in ("claude", "codex", "command"):
        problems.append("the tasks suite runs on claude, codex or command")
    elif executor != "command":
        try:
            cli_argv(settings.get("executor.bin"), executor)
        except ConfigError as exc:
            problems.append(str(exc))
    elif not settings.get("executor.command") or not settings.get("executor.editable"):
        problems.append("command executor needs executor.command and non-empty executor.editable")
    for role in ("executor", "improver", "selector", "reflector", "judge"):
        if role == "executor":
            if executor == "command":
                continue
            values = settings.section(role)
            kind = executor
        else:
            values = settings.role(role)
            kind = values["channel"]
        if kind == "human":
            if role != "selector":
                problems.append("the human channel is only allowed for selector")
            continue
        try:
            if role != "executor":
                _channel(settings, role)
            provider_name, _ = providers.split_model(values["model"])
            problems.extend(providers.check(settings.provider(provider_name)))
        except ConfigError as exc:
            problems.append(f"{role}: {exc}")
    tasks_dir = settings.path("paths.tasks")
    for path in sorted(tasks_dir.iterdir()) if tasks_dir.is_dir() else []:
        if not path.is_dir():
            continue
        try:
            config = _read_task(tasks_dir, path.name)
            if (path / "task.md").read_text(encoding="utf-8").startswith("TODO:"):
                problems.append(f"task {path.name}: task.md still holds the template text")
            if config["workspace"] == "project":
                result = subprocess.run(
                    ["git", "-C", str(ctx.project), "rev-parse", "--show-toplevel"],
                    capture_output=True,
                )
                if result.returncode:
                    problems.append(f"task {path.name}: project workspace needs a git repository")
        except (ConfigError, OSError) as exc:
            problems.append(str(exc))
    return list(dict.fromkeys(problems))
