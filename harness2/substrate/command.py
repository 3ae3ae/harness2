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

"""A shell-command executor with an explicitly editable set of project files."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping

from ..core import runmark as RM
from ..core import tree as HT
from ..core.tree import SurfaceSpec
from ._native import export_files, native_tree, surface_files
from .base import Install
from .codex import _clip


class CommandSubstrate:
    """Run a command template inside the task workspace."""

    name = "command"

    def __init__(self, template: str, editable: tuple[str, ...]):
        if not editable:
            raise ValueError("command executor needs non-empty editable globs")
        self.template = template
        self.editable = editable
        self.NATIVE_PATHS = tuple(f":(glob){g}" for g in editable)

    def surface(self) -> SurfaceSpec:
        return SurfaceSpec(
            name="command",
            always_on=(),
            always_on_max=200_000,
            always_on_cap_reason="",
            skill_dir=".skills-unused",
            skill_name_rule="overrides_dir",
            trigger_hint="",
            autotrigger_file="",
            trigger_line="",
            subagent_requires_mode=False,
            plugin_dir=None,
            hooks_file=None,
            editable_globs=self.editable,
            skills_need_trigger=False,
        )

    def materialise(self, tree_dir: Path, spec, ctx: Mapping[str, str]) -> Install:
        if spec.install_mode != "command_files":
            raise ValueError(
                f"CommandSubstrate needs install_mode 'command_files', got {spec.install_mode!r}"
            )
        staged = Path(tempfile.mkdtemp(prefix="harness2-command-"))
        try:
            self.export_tree(self.import_tree(tree_dir), staged, base={})
            return Install(staged, RM.fingerprint(tree_dir), {"install_mode": spec.install_mode})
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise

    def import_tree(self, root: Path, *, rejected: list[str] | None = None) -> HT.Tree:
        return native_tree(self.surface(), root, (".",), rejected)

    def export_tree(self, tree: HT.Tree, root: Path, *, base: HT.Tree) -> dict[str, list[str]]:
        surface = self.surface()
        return export_files(root, surface_files(surface, tree), surface_files(surface, base))

    def strip_native(self, workspace: Path) -> None:
        self.export_tree({}, workspace, base=self.import_tree(workspace))

    def install_into(self, install: Install, workspace: Path) -> None:
        self.export_tree(self.import_tree(install.staging_dir), workspace, base={})

    def run(
        self,
        prompt_file: Path,
        workspace: Path,
        run_dir: Path,
        timeout: int,
        env: Mapping[str, str],
    ) -> int:
        command = self.template.format(
            workspace=shlex.quote(str(workspace)),
            prompt_file=shlex.quote(str(prompt_file)),
            output=shlex.quote(str(workspace / "output")),
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "stream.log").open("w", encoding="utf-8") as stream:
            return subprocess.run(
                command,
                shell=True,
                cwd=workspace,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                env=dict(env),
            ).returncode

    def read_trajectory(
        self, stream_path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 2000
    ) -> str:
        text = stream_path.with_name("stream.log").read_text(encoding="utf-8", errors="replace")
        tail = text[-obs_trunc:] if obs_trunc > 0 else text
        return _clip(f"[OBSERVATION] {tail}", max_chars)

    def final_message(self, stream_path: Path) -> str:
        return stream_path.with_name("stream.log").read_text(encoding="utf-8", errors="replace")[
            -4000:
        ]

    def cost_usd(self, stream_path: Path) -> float:
        return 0.0
