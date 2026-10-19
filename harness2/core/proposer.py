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

"""Improver sessions with file access in an isolated context workspace.

OpenCode reads output/answer.md or the final message; the Claude CLI reads the
final message. Prompts use stdin, and empty replies raise an error.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import providers
from ..substrate.codex import CodexSubstrate, parse_items_text
from . import events
from .spec import ChannelSpec

SYSTEM_ARG_MAX = 120_000


MAX_CONCURRENCY = 2
_SEM = threading.BoundedSemaphore(MAX_CONCURRENCY)


def _obj(d: dict, key: str) -> dict:
    """`d[key]` when it is a dict, else {}."""
    v = d.get(key)
    return v if isinstance(v, dict) else {}


class ProposerError(RuntimeError):
    """A call did not produce a usable reply. Never swallowed into an empty string."""

    def __init__(self, message: str, *, attempts: int = 0, cost_usd: float = 0.0):
        super().__init__(message)
        self.attempts = attempts
        self.cost_usd = cost_usd


@dataclass
class Reply:
    """One session: what came back, what it cost, and where the audit trail is."""

    label: str
    text: str
    ok: bool
    cost_usd: float = 0.0
    wall_s: float = 0.0
    attempts: int = 0
    cached: bool = False
    channel: str = ""
    error: str = ""
    audit: Path | None = None
    workspace: Path | None = None

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class Proposer:
    """Runs agentic sessions on one channel, with an audit trail and a cache."""

    spec: ChannelSpec
    audit_dir: Path | None = None
    attempts: int = 2
    _calls: list[Reply] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.spec.kind != "human" and not self.spec.model.strip():
            raise ValueError("model must not be empty for model sessions")

    def env(self) -> dict[str, str]:
        e = {k: v for k, v in os.environ.items() if k not in providers.STRIP_ENV}
        if self.spec.provider is not None:
            if self.spec.kind == "claude":
                e.update(providers.claude_env(self.spec.provider))
            elif self.spec.kind == "opencode":
                e.update(providers.opencode_config(self.spec.provider, self._model())[1])
        return e

    def _model(self) -> str:
        return self.spec.model.split("/", 1)[-1]

    def _bin(self) -> list[str]:
        return shlex.split(self.spec.bin) if self.spec.bin else [self.spec.kind]

    def _emit(self, reply: Reply) -> None:
        events.emit(
            "session",
            label=reply.label,
            channel=self.spec.kind,
            model=self.spec.model,
            ok=reply.ok,
            cached=reply.cached,
            cost_usd=reply.cost_usd,
            wall_s=reply.wall_s,
        )

    def call(
        self,
        mission: str,
        context_dir: Path | str | None,
        label: str,
        *,
        system: str = "",
        timeout: int | None = None,
        max_attempts: int | None = None,
        harvest: str | None = None,
    ) -> Reply:
        """Run one session. Raises ProposerError if no usable reply after `attempts`.

        `harvest` names a subpath of the session's working directory to copy out before that
        directory is destroyed -- e.g. "context/harness", which is how an edited tree comes back.
        """

        if self.spec.kind == "human":
            raise ProposerError("the human channel needs an interactive selector")
        cache_key = _call_fingerprint(label, mission, system, context_dir, self.audit_dir)
        cached = self._cached(label, cache_key, require_workspace=bool(harvest))
        if cached is not None:
            self._calls.append(cached)
            self._emit(cached)
            return cached

        wall = int(timeout or self.spec.timeout)
        tries = max(1, int(self.attempts if max_attempts is None else max_attempts))
        last = ""
        spent_usd = 0.0
        t0 = time.time()
        for attempt in range(1, tries + 1):
            if attempt > 1:
                time.sleep(10 * (attempt - 1))
            tmp = Path(tempfile.mkdtemp(prefix="harness2-prop-")).resolve()
            try:
                if context_dir and Path(context_dir).is_dir():
                    shutil.copytree(context_dir, tmp / "context", dirs_exist_ok=True)
                with _SEM if _SEM is not None else contextlib.nullcontext():
                    events.check()
                    text, cost, err = getattr(self, f"_{self.spec.kind}")(
                        tmp, mission, system, wall
                    )
                spent_usd += cost
                if err and providers.USAGE_LIMIT.search(err):
                    events.request_stop("usage_limit")
                    raise events.Stopped("usage_limit")
                if not text.strip():
                    last = err[-300:] or "empty reply"
                    continue
                r = Reply(
                    label=label,
                    text=text,
                    ok=True,
                    cost_usd=spent_usd,
                    wall_s=round(time.time() - t0, 1),
                    attempts=attempt,
                    channel=self.spec.kind,
                )
                if harvest:
                    r.workspace = self._harvest(tmp, harvest, label, cache_key)
                    if r.workspace is None:
                        _drop_workspace(self._paths(label, cache_key))
                self._write_audit(r, mission, system, cache_key)
                self._calls.append(r)
                self._emit(r)
                return r
            except events.Stopped:
                raise
            except subprocess.TimeoutExpired:
                last = f"timeout after {wall}s"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            if providers.USAGE_LIMIT.search(last):
                events.request_stop("usage_limit")
                raise events.Stopped("usage_limit")

        r = Reply(
            label=label,
            text="",
            ok=False,
            wall_s=round(time.time() - t0, 1),
            attempts=tries,
            channel=self.spec.kind,
            error=last,
            cost_usd=spent_usd,
        )
        self._calls.append(r)
        self._write_audit(r, mission, system, cache_key)
        self._emit(r)
        raise ProposerError(
            f"[{label}] {self.spec.kind}/{self.spec.model}: {last}",
            attempts=tries,
            cost_usd=spent_usd,
        )

    def _claude(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        login = self.spec.provider is not None and self.spec.provider.type == "claude-login"
        env = self.env()
        if not login:
            home = cwd / ".claude-home"
            home.mkdir()
            env["CLAUDE_CONFIG_DIR"] = str(home)
        cmd = [
            *self._bin(),
            "--dangerously-skip-permissions",
            "-p",
            "--output-format",
            "json",
            "--effort",
            self.spec.effort,
            "--model",
            self._model(),
            "--setting-sources",
            "project,local",
            *(["--strict-mcp-config", "--no-session-persistence"] if login else []),
        ]
        stdin = mission
        if system:
            if len(system) > SYSTEM_ARG_MAX:
                stdin = f"# SYSTEM INSTRUCTIONS\n\n{system}\n\n# END\n\n{mission}"
                cmd += [
                    "--append-system-prompt",
                    "Follow the SYSTEM INSTRUCTIONS block at the top of the message.",
                ]
            else:
                cmd += ["--append-system-prompt", system]
        p = subprocess.run(
            cmd,
            input=stdin,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=wall,
        )
        payload = _parse_json(p.stdout)
        text = str(payload.get("result") or payload.get("text") or "")
        err = ""
        if (
            p.returncode
            or payload.get("is_error")
            or payload.get("subtype") not in (None, "success")
        ):
            err = f"rc={p.returncode}: {p.stdout} {p.stderr}"
            text = ""
        return text, float(payload.get("total_cost_usd") or 0.0), err

    def _codex(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        model = self._model()
        home = None
        if self.spec.provider is None or self.spec.provider.type != "chatgpt-login":
            home = cwd / ".codex-home"
            home.mkdir()
            config = (
                providers.codex_config(self.spec.provider, model, self.spec.effort)
                if self.spec.provider is not None
                else f"model = {json.dumps(model)}\n"
                f"model_reasoning_effort = {json.dumps(self.spec.effort)}\n"
            )
            (home / "config.toml").write_text(config, encoding="utf-8")
        p = subprocess.run(
            [
                *self._bin(),
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "--ephemeral",
                *providers.codex_args(self.spec.provider, model, self.spec.effort),
                "-C",
                str(cwd),
                "-",
            ],
            input=f"# SYSTEM INSTRUCTIONS\n\n{system}\n\n# END\n\n{mission}" if system else mission,
            cwd=cwd,
            env=CodexSubstrate().env(self.env(), home, provider=self.spec.provider),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=wall,
        )
        text, errors = "", []
        for event in parse_items_text(p.stdout):
            if event.get("type") in ("turn.failed", "error"):
                error = event.get("error") or event.get("message") or event["type"]
                errors.append(str(error))
            item = _obj(event, "item")
            if item.get("type") == "agent_message":
                text = str(item.get("text") or "")
        if errors or p.returncode:
            return "", 0.0, f"rc={p.returncode}: {p.stdout} {p.stderr}"
        return text, 0.0, ""

    def _opencode(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        (cwd / "MISSION.md").write_text(
            (f"{system}\n\n---\n\n" if system else "") + mission, encoding="utf-8"
        )
        out = cwd / "output"
        out.mkdir(exist_ok=True)

        config = {"agent": {"build": {"steps": self.spec.steps}}}
        if self.spec.provider is not None:
            config.update(providers.opencode_config(self.spec.provider, self._model())[0])
        (cwd / "opencode.json").write_text(json.dumps(config), encoding="utf-8")
        prompt = (
            "Read MISSION.md first. Do what it asks. Reply with the requested output as "
            "your final message AND save it verbatim to output/answer.md."
        )

        env = self.env()
        env["OPENCODE_PERMISSION"] = json.dumps(
            {"external_directory": {"*": "deny", str(cwd): "allow", f"{cwd}/**": "allow"}}
        )
        env["XDG_DATA_HOME"] = str(cwd / ".ocdata")
        env["XDG_STATE_HOME"] = str(cwd / ".ocstate")

        cmd = [
            *self._bin(),
            "run",
            prompt,
            "--dir",
            str(cwd),
            "--model",
            self.spec.model,
            "--format",
            "json",
        ]
        if self.spec.effort:
            cmd += ["--variant", self.spec.effort]
        p = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=wall,
        )
        cost = _opencode_cost(p.stdout)
        text, err = _opencode_stdout(p.stdout)
        if (p.returncode or err) and providers.USAGE_LIMIT.search(p.stdout + p.stderr):
            return "", cost, p.stdout + p.stderr
        ans = out / "answer.md"
        if ans.is_file() and ans.read_text(errors="replace").strip():
            return ans.read_text(errors="replace"), cost, ""
        if text.strip():
            return text, cost, ""
        tail = _strip_oc_banner(p.stderr or "").strip()[-300:]
        return "", cost, f"rc={p.returncode}: {err or tail or 'empty reply'}"

    def _harvest(self, tmp: Path, sub: str, label: str, cache_key: str = "") -> Path | None:
        """Copy `tmp/<sub>` somewhere durable before the session directory is removed."""
        src = tmp / sub
        if not src.is_dir():
            return None
        dest = Path(self.audit_dir or tempfile.mkdtemp(prefix="harness2-harvest-"))
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
        if cache_key:
            safe = f"{safe}.{cache_key}"
        dest = dest / f"{safe}.workspace"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
        return dest

    def _paths(self, label: str, cache_key: str = "") -> dict[str, Path] | None:
        if not self.audit_dir:
            return None
        d = Path(self.audit_dir)
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
        if cache_key:
            safe = f"{safe}.{cache_key}"
        return {k: d / f"{safe}.{k}.md" for k in ("mission", "system", "response")} | {
            "meta": d / f"{safe}.meta.json",
            "workspace": d / f"{safe}.workspace",
        }

    def _cached(
        self, label: str, cache_key: str = "", *, require_workspace: bool = False
    ) -> Reply | None:
        p = self._paths(label, cache_key)
        if not p or not p["response"].is_file():
            return None
        if require_workspace and not p["workspace"].is_dir():
            return None
        txt = p["response"].read_text(encoding="utf-8", errors="replace")
        if not txt.strip():
            return None
        return Reply(
            label=label,
            text=txt,
            ok=True,
            cost_usd=0.0,
            cached=True,
            channel=self.spec.kind,
            audit=p["response"],
            workspace=p["workspace"] if p["workspace"].is_dir() else None,
        )

    def _write_audit(self, r: Reply, mission: str, system: str, cache_key: str = "") -> None:
        p = self._paths(r.label, cache_key)
        if not p:
            return
        p["mission"].write_text(mission, encoding="utf-8")
        if system:
            p["system"].write_text(system, encoding="utf-8")
        p["response"].write_text(r.text, encoding="utf-8")
        p["meta"].write_text(
            json.dumps(
                {
                    "label": r.label,
                    "ok": r.ok,
                    "channel": r.channel,
                    "model": self.spec.model,
                    "effort": self.spec.effort,
                    "attempts": r.attempts,
                    "wall_s": r.wall_s,
                    "cost_usd": r.cost_usd,
                    "error": r.error,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        r.audit = p["response"]


def _drop_workspace(paths: dict[str, Path] | None) -> None:
    """Remove a stale harvested workspace so a later cache read cannot serve it."""
    if paths:
        shutil.rmtree(paths["workspace"], ignore_errors=True)


def _parse_json(stdout: str) -> dict:
    """The claude CLI's one JSON object, tolerating banners and trailing noise around it.

    Not for opencode stdout, which is a JSONL event stream: use `_opencode_stdout`.
    """
    s = (stdout or "").strip()
    if not s:
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        for line in reversed(s.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        i, j = s.find("{"), s.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(s[i : j + 1])
            except json.JSONDecodeError:
                pass
    return {"result": s}


_OC_BANNER = re.compile(
    r"^(?:Performing one time database migration[^\n]*|sqlite-migration:[^\n]*"
    r"|Database migration complete\.)[ \t]*$",
    re.M,
)


def _strip_oc_banner(s: str) -> str:
    """Drop the migration banner lines from OpenCode output. No-op when they are absent."""
    return _OC_BANNER.sub("", s or "")


def _opencode_stdout(stdout: str) -> tuple[str, str]:
    """Fold `opencode run --format json` into (final assistant text, error text).

    The stream is one raw event per line and assistant prose lives at part.text of a `text`
    event. All text events are joined, so a trailing terminator event cannot swallow the
    answer; `error` events become the error string. Plain-text stdout is returned as-is.
    """
    texts: list[str] = []
    errors: list[str] = []
    saw_json = False
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        saw_json = True
        part = _obj(ev, "part")
        ptype = part.get("type") or ev.get("type")
        if ptype == "text":
            t = part.get("text")
            if isinstance(t, str) and t.strip():
                texts.append(t)
        elif ptype == "error":
            e = ev.get("error")
            if isinstance(e, dict):
                msg = str(_obj(e, "data").get("message") or e.get("name") or "").strip()
            else:
                msg = str(e or "").strip()
            if msg:
                errors.append(msg)
    if not saw_json:
        return _strip_oc_banner(stdout).strip(), ""
    return "\n\n".join(texts), "; ".join(errors)[-300:]


def _opencode_cost(stdout: str) -> float:
    """Sum `part.cost` over the step_finish events of an `opencode run --format json` stream."""
    total = 0.0
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        part = _obj(ev, "part")
        if (part.get("type") or ev.get("type")) in ("step-finish", "step_finish"):
            c = part.get("cost")
            if isinstance(c, (int, float)):
                total += float(c)
    return round(total, 6)


def _call_fingerprint(
    label: str,
    mission: str,
    system: str,
    context_dir: Path | str | None,
    anchor: Path | None = None,
) -> str:
    """Content-address one call, including its evidence and its mode/task workspace path.

    The resolved path is part of the key: the two modes can have byte-identical k=1 evidence
    and must still be independent draws.
    """
    h = hashlib.sha256()
    for value in (label, mission, system):
        h.update(value.encode("utf-8", errors="replace"))
        h.update(b"\0")
    if context_dir:
        root = Path(context_dir)
        location = (
            os.path.relpath(root.resolve(), Path(anchor).resolve())
            if anchor
            else str(root.resolve())
        )
        h.update(location.encode("utf-8", errors="replace"))
        h.update(b"\0")
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if not p.is_file() or "__pycache__" in p.parts:
                    continue
                h.update(str(p.relative_to(root)).encode("utf-8", errors="replace"))
                h.update(b"\0")
                try:
                    with p.open("rb") as fh:
                        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                            h.update(chunk)
                except OSError as exc:
                    h.update(f"<unreadable:{type(exc).__name__}:{exc}>".encode())
                h.update(b"\0")
    return h.hexdigest()[:16]
