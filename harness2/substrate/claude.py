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

"""Claude Code's editable surface, native project files and stream-json renderer."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Mapping

from .. import providers
from ..core import runmark as RM
from ..core import tree as HT
from ..core.tree import SurfaceSpec
from ..settings import ConfigError
from ._native import check_cli, checked_path, cli_argv, export_files, native_tree, surface_files
from .base import Install
from .codex import _TRUNC_FMT, _clip, _text_of

CLAUDE_SURFACE = SurfaceSpec(
    name="claude",
    always_on=("CLAUDE.md",),
    always_on_max=40_000,
    always_on_cap_reason="CLAUDE.md is loaded whole into every session, and Claude Code "
    "warns above 40,000 characters",
    skill_dir=".claude/skills",
    skill_name_rule="overrides_dir",
    trigger_hint="",
    autotrigger_file="CLAUDE.md",
    trigger_line="",
    subagent_requires_mode=False,
    plugin_dir=None,
    hooks_file=".claude/hooks.json",
    hook_events=frozenset(
        {
            "PreToolUse",
            "PostToolUse",
            "Notification",
            "UserPromptSubmit",
            "Stop",
            "SubagentStop",
            "PreCompact",
            "SessionStart",
            "SessionEnd",
        }
    ),
    compile_skill_scripts=True,
    reject_empty_always_on=False,
    script_dir=".claude/scripts",
    skills_need_trigger=False,
    stubs=(),
)

NATIVE_PATHS = (
    "CLAUDE.md",
    ".claude/skills",
    ".claude/agents",
    ".claude/scripts",
    ".claude/settings.json",
)
_SETTINGS = ".claude/settings.json"


def _settings(root: Path) -> dict:
    path = checked_path(root, _SETTINGS)
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return doc


def _agent_body(name: str, body: str) -> str:
    frontmatter = re.match(r"\A---\r?\n(.*?)^---(?:\r?\n|$)", body, re.S | re.M)
    if frontmatter:
        name_line = re.search(r"^name:[ \t]*(.*)$", frontmatter.group(1), re.M)
        if name_line and name_line.group(1).strip():
            return body
        if name_line:
            start = frontmatter.start(1) + name_line.start()
            end = frontmatter.start(1) + name_line.end()
            return body[:start] + f"name: {name}" + body[end:]
        return body[: frontmatter.start(1)] + f"name: {name}\n" + body[frontmatter.start(1) :]
    return f"---\nname: {name}\n---\n{body}"


def _native_files(tree: HT.Tree) -> tuple[HT.Tree, dict | None]:
    files = {}
    hooks = None
    for rel, content in surface_files(CLAUDE_SURFACE, tree).items():
        if rel == CLAUDE_SURFACE.hooks_file:
            try:
                doc = json.loads(content)
            except ValueError as exc:
                raise ConfigError(f"{rel} is not valid JSON: {exc}") from exc
            if not isinstance(doc, dict) or not isinstance(doc.get("hooks"), dict):
                raise ConfigError(f'{rel} needs a top-level "hooks" object')
            hooks = doc["hooks"]
        elif rel.startswith("sub_agents/"):
            name = rel.split("/")[1]
            files[f".claude/agents/{name}.md"] = _agent_body(name, content)
        else:
            files[rel] = content
    return files, hooks


class ClaudeSubstrate:
    """Claude Code with isolated per-run global configuration."""

    name = "claude"
    NATIVE_PATHS = NATIVE_PATHS

    def __init__(self, bin: str = ""):
        self.bin = bin

    def surface(self) -> SurfaceSpec:
        return CLAUDE_SURFACE

    def materialise(self, tree_dir: Path, spec, ctx: Mapping[str, str]) -> Install:
        if spec.install_mode != "claude_root":
            raise ValueError(
                f"ClaudeSubstrate needs install_mode 'claude_root', got {spec.install_mode!r}"
            )
        staged = Path(tempfile.mkdtemp(prefix="harness2-claude-"))
        try:
            self.export_tree(HT.read_tree(CLAUDE_SURFACE, tree_dir), staged, base={})
            return Install(staged, RM.fingerprint(tree_dir), {"install_mode": spec.install_mode})
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise

    def import_tree(self, root: Path, *, rejected: list[str] | None = None) -> HT.Tree:
        tree = native_tree(
            CLAUDE_SURFACE, root, ("CLAUDE.md", ".claude/skills", ".claude/scripts"), rejected
        )
        agents = checked_path(root, ".claude/agents")
        for path in sorted(agents.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            canonical = f"sub_agents/{path.stem}/AGENT.md"
            if (
                path.parent == agents
                and path.suffix == ".md"
                and HT.is_allowed(CLAUDE_SURFACE, canonical)
            ):
                tree[canonical] = checked_path(root, rel).read_text(
                    encoding="utf-8", errors="replace"
                )
            elif rejected is not None:
                rejected.append(rel)
        settings = _settings(root)
        if "hooks" in settings:
            if not isinstance(settings["hooks"], dict):
                raise ConfigError(f'{_SETTINGS} needs a "hooks" object')
            tree[CLAUDE_SURFACE.hooks_file] = (
                json.dumps({"hooks": settings["hooks"]}, indent=2) + "\n"
            )
        return tree

    def export_tree(self, tree: HT.Tree, root: Path, *, base: HT.Tree) -> dict[str, list[str]]:
        files, hooks = _native_files(tree)
        old, old_hooks = _native_files(base)
        if hooks is not None or old_hooks is not None:
            settings = _settings(root)
            merged = dict(settings)
            if hooks is None:
                merged.pop("hooks", None)
            else:
                merged["hooks"] = hooks
            if merged != settings:
                files[_SETTINGS] = json.dumps(merged, indent=2) + "\n"
        return export_files(root, files, old)

    def strip_native(self, workspace: Path) -> None:
        self.export_tree({}, workspace, base=self.import_tree(workspace))

    def install_into(self, install: Install, workspace: Path) -> None:
        self.export_tree(self.import_tree(install.staging_dir), workspace, base={})

    def argv(
        self,
        prompt_file: Path,
        workspace: Path,
        model: str,
        effort: str,
        *,
        provider: providers.Provider | None = None,
    ) -> list[str]:
        return [
            *cli_argv(self.bin, "claude"),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--setting-sources",
            "project,local",
            "--model",
            model,
            "--effort",
            effort,
            *(
                ["--strict-mcp-config", "--no-session-persistence"]
                if provider is not None and provider.type == "claude-login"
                else []
            ),
        ]

    def env(
        self, base_env: Mapping[str, str], provider: providers.Provider, config_dir: Path
    ) -> dict[str, str]:
        login = provider.type == "claude-login"
        if not login:
            config_dir.mkdir(parents=True, exist_ok=True)
        return {
            **{k: v for k, v in base_env.items() if k not in providers.STRIP_ENV},
            **providers.claude_env(provider),
            **({} if login else {"CLAUDE_CONFIG_DIR": str(config_dir)}),
        }

    def read_trajectory(
        self, stream_path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 2000
    ) -> str:
        return render_events(
            stream_path, obs_trunc=obs_trunc, max_chars=max_chars, args_trunc=args_trunc
        )

    def final_message(self, stream_path: Path) -> str:
        return final_message(stream_path)

    def cost_usd(self, stream_path: Path) -> float:
        return cost_usd(stream_path)

    def preflight(self) -> None:
        check_cli(self.bin, "claude")


def parse_events(path: Path) -> list[dict]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").replace("\x00", "")
    except OSError as exc:
        return [{"type": "_read_error", "message": str(exc)}]
    events = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def render_events(path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 2000) -> str:
    lines = []
    for event in parse_events(path):
        kind = event.get("type")
        if kind == "_read_error":
            return f"(failed to read trajectory: {event.get('message')})"
        if kind == "system":
            if event.get("subtype") == "init":
                lines.append(f"[HARNESS] init | model={event.get('model')} cwd={event.get('cwd')}")
            continue
        if kind == "result":
            if event.get("is_error"):
                text = event.get("result") or _text_of(event.get("errors")) or event.get("subtype")
                lines.append(f"[ERROR] {_clip(text, obs_trunc)}")
            elif event.get("result"):
                lines.append(f"[ASSISTANT] {_clip(event['result'], obs_trunc)}")
            continue
        message = event.get("message")
        if kind not in ("assistant", "user") or not isinstance(message, dict):
            continue
        content = message.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if kind == "assistant" and block_type in ("text", "thinking"):
                tag = "ASSISTANT" if block_type == "text" else "REASONING"
                text = block.get("text" if block_type == "text" else "thinking")
                lines.append(f"[{tag}] {_clip(text, obs_trunc)}")
            elif kind == "assistant" and block_type == "tool_use":
                args = json.dumps(block.get("input", {}), ensure_ascii=False)
                lines.append(f"[ACTION] {block.get('name', 'tool')}: {_clip(args, args_trunc)}")
            elif kind == "user" and block_type == "tool_result":
                tag = "OBSERVATION ERROR" if block.get("is_error") else "OBSERVATION"
                lines.append(f"[{tag}] {_clip(_text_of(block.get('content')), obs_trunc)}")
            elif kind == "user" and block_type == "text":
                lines.append(f"[USER] {_clip(block.get('text'), obs_trunc)}")
    rendered = "\n".join(lines).strip()
    if not rendered:
        return "(no parseable events in trajectory)"
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "\n" + _TRUNC_FMT.format(n=len(rendered) - max_chars)
    return rendered


def final_message(path: Path) -> str:
    last = ""
    result = None
    for event in parse_events(path):
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            result = event["result"]
        elif event.get("type") == "assistant" and isinstance(event.get("message"), dict):
            text = _text_of(event["message"].get("content"))
            if text.strip():
                last = text
    return result if result is not None else last


def cost_usd(path: Path) -> float:
    for event in reversed(parse_events(path)):
        if event.get("type") == "result":
            value = event.get("total_cost_usd")
            return float(value) if isinstance(value, (int, float)) else 0.0
    return 0.0
