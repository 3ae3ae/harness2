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

"""Temporary project worktrees and file workspaces for rollouts and judges."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .settings import ConfigError


@contextmanager
def project_workspace(project: Path, ref: str) -> Iterator[Path]:
    ws = Path(tempfile.mkdtemp(prefix="harness2-ws-")).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(project), "worktree", "add", "--detach", "--", str(ws), ref],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise ConfigError(
                f"cannot create project workspace at {ref!r}: {result.stderr.strip()}"
            )
        yield ws
    finally:
        subprocess.run(
            ["git", "-C", str(project), "worktree", "remove", "--force", str(ws)],
            capture_output=True,
        )
        shutil.rmtree(ws, ignore_errors=True)


@contextmanager
def files_workspace(src: Path | None) -> Iterator[Path]:
    ws = Path(tempfile.mkdtemp(prefix="harness2-ws-")).resolve()
    try:
        if src is not None:
            shutil.copytree(src, ws, dirs_exist_ok=True)
        (ws / "output").mkdir(exist_ok=True)
        yield ws
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def commit_all(ws: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=harness2",
            "-c",
            "user.email=harness2@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-verify",
            "--allow-empty",
            "-qm",
            message,
        ],
        cwd=ws,
        check=True,
        capture_output=True,
    )


def diff_since(ws: Path, base: str, exclude: Sequence[str]) -> str:
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True, capture_output=True)
    pathspecs = [
        p.replace(":(", ":(exclude,", 1) if p.startswith(":(") else f":(exclude){p}"
        for p in exclude
    ]
    return subprocess.run(
        ["git", "diff", "--cached", "--binary", base, "--", ".", *pathspecs],
        cwd=ws,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def apply_patch(ws: Path, patch: str) -> None:
    if patch.strip():
        subprocess.run(
            ["git", "apply", "--whitespace=nowarn"],
            cwd=ws,
            input=patch,
            text=True,
            check=True,
            capture_output=True,
        )
