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

"""Offline workspace isolation, binary patches, and git cleanup."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from harness2 import workspace
from harness2.settings import ConfigError


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / "code.txt").write_text("before\n")
        (self.project / "CLAUDE.md").write_text("instructions\n")
        workspace.commit_all(self.project, "initial")

    def test_concurrent_project_workspaces(self):
        def open_one(_):
            with workspace.project_workspace(self.project, "HEAD") as ws:
                return (ws / "code.txt").read_text()

        with ThreadPoolExecutor(8) as pool:
            self.assertEqual(list(pool.map(open_one, range(16))), ["before\n"] * 16)
        listed = subprocess.run(
            ["git", "-C", str(self.project), "worktree", "list"], capture_output=True, text=True
        )
        self.assertEqual(len(listed.stdout.splitlines()), 1)

    def test_project_diff_apply_and_cleanup(self):
        with workspace.project_workspace(self.project, "HEAD") as ws:
            self.assertFalse(ws.is_relative_to(self.project))
            workspace.commit_all(ws, "empty candidate")
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ws, text=True).strip()
            (ws / "code.txt").write_text("after\n")
            (ws / "new.bin").write_bytes(b"\0\xffbinary")
            (ws / "CLAUDE.md").write_text("private changes\n")
            (ws / ".harness2").mkdir()
            (ws / ".harness2/secret").write_text("secret")
            patch = workspace.diff_since(ws, base, ["CLAUDE.md", ".harness2"])
            self.assertIn("GIT binary patch", patch)
            self.assertNotIn("CLAUDE.md", patch)
            self.assertNotIn(".harness2", patch)
        self.assertFalse(ws.exists())
        with workspace.project_workspace(self.project, "HEAD") as judge:
            workspace.apply_patch(judge, patch)
            workspace.apply_patch(judge, "")
            self.assertEqual((judge / "code.txt").read_text(), "after\n")
            self.assertEqual((judge / "new.bin").read_bytes(), b"\0\xffbinary")
            self.assertEqual((judge / "CLAUDE.md").read_text(), "instructions\n")
            with self.assertRaises(subprocess.CalledProcessError):
                workspace.apply_patch(judge, "broken patch")
        self.assertEqual((self.project / "code.txt").read_text(), "before\n")
        listing = subprocess.check_output(
            ["git", "worktree", "list", "--porcelain"], cwd=self.project, text=True
        )
        self.assertEqual(listing.count("worktree "), 1)

    def test_glob_exclusions_and_cleanup_on_error(self):
        with self.assertRaisesRegex(RuntimeError, "test"):
            with workspace.project_workspace(self.project, "HEAD") as ws:
                (ws / "CLAUDE.md").write_text("new instructions")
                (ws / "code.txt").write_text("after")
                patch = workspace.diff_since(ws, "HEAD", [":(glob)*.md"])
                self.assertNotIn("CLAUDE.md", patch)
                self.assertIn("code.txt", patch)
                raise RuntimeError("test")
        self.assertFalse(ws.exists())
        for project, ref in ((self.project, "missing-ref"), (self.root, "HEAD")):
            with self.assertRaises(ConfigError):
                with workspace.project_workspace(project, ref):
                    self.fail("invalid project/ref accepted")

    def test_files_copy_output_and_cleanup(self):
        src = self.root / "files"
        src.mkdir()
        (src / "data.csv").write_text("amount\n1\n")
        with workspace.files_workspace(src) as ws:
            self.assertTrue((ws / "output").is_dir())
            (ws / "data.csv").write_text("changed")
        self.assertFalse(ws.exists())
        self.assertEqual((src / "data.csv").read_text(), "amount\n1\n")
        with workspace.files_workspace(None) as ws:
            self.assertEqual([p.name for p in ws.iterdir()], ["output"])
