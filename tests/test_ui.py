"""The interactive commands: init, status, apply and the human selector."""

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2.cli import apply, init, status
from harness2.core.spec import ChannelSpec


class _Ctx:
    def __init__(self, project: Path):
        self.project = project
        self.settings = type("S", (), {"path": lambda _self, key: project})()
        self.logged: list[str] = []

    def log(self, message):
        self.logged.append(message)

    def out(self, data, text):
        print(text(data))


class UiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_human_selector_returns_the_typed_choice(self):
        from harness2.cli.human import HumanSelector

        context = self.root / "context"
        (context / "candidates").mkdir(parents=True)
        (context / "task.md").write_text("Fix the bug.\n")
        for anon in ("A", "B"):
            (context / "candidates" / f"{anon}.md").write_text(f"candidate {anon}\n")
        selector = HumanSelector(ChannelSpec("human", ""))
        with (
            patch.object(sys.stdin, "isatty", return_value=True),
            patch("builtins.input", side_effect=["C", "B"]),
            patch("sys.stderr"),
        ):
            reply = selector.call("mission", context, "select.agg2")
        self.assertIn("```choice\nB\n```", reply.text)

    def test_apply_keeps_project_edits_made_since_the_run_started(self):
        base = {"CLAUDE.md": "base\n", "a.md": "a\n", "b.md": "b\n"}
        adopted = {"CLAUDE.md": "base\nrun rule\n", "a.md": "a run\n", "b.md": "b\n"}
        current = {"CLAUDE.md": "base\nmy rule\n", "a.md": "a\n", "b.md": "b mine\n"}
        written = {}

        def perform(settings, adapter, tree, *, base, branch):
            written.update(tree)
            return {"written": sorted(tree), "deleted": []}

        args = argparse.Namespace(
            name=None,
            task=None,
            mode="parallel",
            interactive=False,
            yes=True,
            branch=None,
            dry_run=False,
            force=False,
        )
        with (
            patch.object(apply.A, "open_run", return_value=(None, self.root / "run")),
            patch.object(apply.A, "adopted_tree", return_value=("t", adopted)),
            patch.object(apply.A, "base_tree", return_value=base),
            patch.object(apply.A, "project_tree", return_value=current),
            patch.object(apply.A, "dirty_paths", return_value=[]),
            patch.object(apply.A, "perform", side_effect=perform),
            patch("sys.stdout"),
        ):
            self.assertEqual(apply.run(args, _Ctx(self.root)), 0)
        # CLAUDE.md changed on both sides: skipped. a.md changed by the run only: applied.
        # b.md changed by the project only: kept.
        self.assertEqual(
            written, {"CLAUDE.md": "base\nmy rule\n", "a.md": "a run\n", "b.md": "b mine\n"}
        )

    def test_status_reads_tasks_and_spend_from_events(self):
        run = self.root / "run"
        run.mkdir()
        (run / "run.json").write_text(
            json.dumps(
                {
                    "name": "r",
                    "suite": "tasks",
                    "executor": "claude",
                    "settings": {},
                    "schedule": {"k": 2, "modes": "parallel"},
                    "created": "2026-01-01T00:00:00Z",
                    "stream": [["d", "t1"], ["d", "t2"], ["d", "t3"]],
                }
            )
        )
        events = [
            {"kind": "run_start", "t": "2026-01-01T00:00:01Z"},
            {"kind": "task_start", "task": "t1", "t": "2026-01-01T00:00:02Z"},
            {"kind": "rollout", "task": "t1", "cost_usd": 0.5, "t": "2026-01-01T00:00:03Z"},
            {"kind": "session", "cost_usd": 0.25, "cached": False, "t": "2026-01-01T00:00:04Z"},
            {"kind": "selection", "task": "t1", "mode": "agg", "chosen": "agg2"},
            {"kind": "task_done", "task": "t1", "status": "ok"},
            {"kind": "task_start", "task": "t2"},
            {"kind": "run_end", "status": "stopped", "reason": "max_calls"},
        ]
        (run / "events.jsonl").write_text("\n".join(map(json.dumps, events)) + "\n{partial")
        with patch.object(
            status.C, "load_run", side_effect=lambda d: json.loads((d / "run.json").read_text())
        ):
            snap = status.snapshot(run)
        states = {row["task"]: row["state"] for row in snap["tasks"]}
        self.assertEqual(states, {"t1": "done", "t2": "stopped", "t3": "pending"})
        self.assertEqual((snap["spent_usd"], snap["calls"]), (0.75, 2))
        self.assertEqual(snap["tasks"][0]["picked"], {"parallel": "agg2"})
        text = status.render(snap)
        self.assertIn("1 done · 1 stopped · 1 pending", text)
        self.assertIn("run ended: stopped (max_calls) · run the same command to resume", text)

    def test_init_writes_a_loadable_config_once(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text("#!/bin/sh\necho 'fake claude 1.0'\n")
        fake.chmod(0o755)
        project = self.root / "project"
        project.mkdir()
        args = argparse.Namespace(
            project=str(project), yes=True, force=False, executor=None, auth=None
        )
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }
        with patch.dict(os.environ, env, clear=True), patch("sys.stdout"):
            self.assertEqual(init.run(args, None), 0)
            from harness2 import settings

            loaded = settings.load(project)
            self.assertEqual(loaded.get("executor.name"), "claude")
            self.assertEqual(loaded.get("improver.model"), "claude-login/claude-opus-5-5")
            self.assertTrue((project / ".harness2/tasks").is_dir())
            self.assertIn("runs/", (project / ".harness2/.gitignore").read_text())
            with patch("sys.stderr"):
                self.assertEqual(init.run(args, None), 1)


if __name__ == "__main__":
    unittest.main()
