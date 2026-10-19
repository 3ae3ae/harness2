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

"""Shared CLI resolution and bounded native-file operations for executors."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

from ..core import tree as HT
from ..settings import ConfigError


def cli_argv(binary: str, default: str) -> list[str]:
    argv = shlex.split(binary or default)
    resolved = shutil.which(argv[0]) if argv else None
    if resolved is None:
        raise ConfigError(f"executor binary not found: {binary or default}")
    return [resolved, *argv[1:]]


def check_cli(binary: str, default: str) -> None:
    argv = cli_argv(binary, default)
    try:
        subprocess.run([*argv, "--version"], check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigError(f"executor version check failed: {binary or default}") from exc


def checked_path(root: Path, rel: str) -> Path:
    rel = HT.normalize_rel(rel)
    if not rel or rel == ".":
        raise ValueError("managed file path must not be empty")
    path = root
    for part in Path(rel).parts:
        path = path / part
        if path.is_symlink():
            raise ConfigError(f"managed path is a symlink: {rel}")
    return path


def surface_files(surface: HT.SurfaceSpec, tree: HT.Tree) -> HT.Tree:
    stubs = {rel for rel, _ in surface.stubs}
    out = {}
    for rel, content in tree.items():
        rel = HT.normalize_rel(rel)
        if rel in stubs:
            continue
        if not HT.is_allowed(surface, rel):
            raise ValueError(f"file is outside the {surface.name} surface: {rel}")
        out[rel] = content
    return out


def native_tree(
    surface: HT.SurfaceSpec,
    root: Path,
    entries: tuple[str, ...],
    rejected: list[str] | None = None,
) -> HT.Tree:
    out = {}
    stubs = {rel for rel, _ in surface.stubs}
    for entry in entries:
        path = root if entry == "." else checked_path(root, entry)
        candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
        for candidate in candidates:
            if not candidate.is_file() or "__pycache__" in candidate.parts:
                continue
            rel = candidate.relative_to(root).as_posix()
            if rel in stubs:
                continue
            if HT.is_allowed(surface, rel):
                out[rel] = checked_path(root, rel).read_text(encoding="utf-8", errors="replace")
            elif rejected is not None:
                rejected.append(rel)
    return out


def export_files(root: Path, files: HT.Tree, base: HT.Tree) -> dict[str, list[str]]:
    paths = {rel: checked_path(root, rel) for rel in files.keys() | base.keys()}
    written = []
    deleted = []
    for rel, content in sorted(files.items()):
        path = paths[rel]
        if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == content:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(rel)
    for rel in sorted(base.keys() - files.keys()):
        path = paths[rel]
        if path.is_file():
            path.unlink()
            deleted.append(rel)
    return {"written": written, "deleted": deleted}
