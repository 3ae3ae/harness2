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

"""Offline checks for machine output and run process controls."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from harness2.cli import _adopt, diff, main, run
from harness2.cli import _common as C
from harness2.core import events
from harness2.core.tree import diff_summary
from harness2.demo import DemoAdapter, DemoProposer


class AgentOpsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        (self.project / ".harness2").mkdir()
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.project / "config")}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(events.start, None)
        self.root = self.project / ".harness2/runs/claude-k1"

        def make_adapter(ctx):
            adapter = DemoAdapter(ctx.run_root / "fixture")
            adapter.spec = replace(adapter.spec, runs_root=ctx.run_root)
            return adapter

        self.suite = Mock(make_adapter=Mock(side_effect=make_adapter), check=Mock(return_value=[]))
        registry = patch("harness2.registry.load", return_value=self.suite)
        registry.start()
        self.addCleanup(registry.stop)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main.main(["-C", str(self.project), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def pid_record(self, root=None):
        root = root or self.root
        root.mkdir(parents=True, exist_ok=True)
        record = {"pid": os.getpid(), "started": "2026-10-19T00:00:00+00:00", "argv": ["run"]}
        (root / "run.pid").write_text(json.dumps(record))
        return record

    def test_json_errors_and_text_behavior(self):
        for exception in (
            OSError("io"),
            RuntimeError("runtime"),
            ValueError("value"),
            KeyError("key"),
        ):
            with (
                self.subTest(exception=exception),
                patch.object(diff, "run", side_effect=exception),
            ):
                code, out, err = self.invoke("--json", "--quiet", "diff")
                self.assertEqual(code, 1)
                self.assertEqual(
                    out.splitlines(), [json.dumps({"error": str(exception), "exit_code": 1})]
                )
                self.assertEqual(err, "")
                self.assertEqual(self.invoke("diff"), (1, "", f"error: {exception}\n"))
                self.assertEqual(self.invoke("--quiet", "diff"), (1, "", ""))
        with patch.object(diff, "run", side_effect=KeyboardInterrupt):
            self.assertEqual(self.invoke("diff"), (130, "", ""))
            code, out, err = self.invoke("--json", "diff")
        self.assertEqual(
            (code, json.loads(out), err), (130, {"error": "interrupted", "exit_code": 130}, "")
        )

    def test_argparse_errors_still_exit_two(self):
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            with self.assertRaises(SystemExit) as raised:
                main.main(["--json", "run", "--unknown"])
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("usage: harness2", err.getvalue())

    def test_detach_preserves_in_process_argv_and_appends_logs(self):
        child = Mock(pid=12345)
        child.wait.side_effect = subprocess.TimeoutExpired("harness2", 2)

        def popen(argv, **kwargs):
            self.assertEqual(
                argv,
                [
                    sys.executable,
                    "-m",
                    "harness2",
                    "-C",
                    str(self.project),
                    "--json",
                    "run",
                    "--seed",
                    "0",
                    "--name",
                    "claude-k1-s0",
                ],
            )
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
            self.assertTrue(kwargs["start_new_session"])
            self.assertNotIn("cwd", kwargs)
            self.assertNotIn("env", kwargs)
            kwargs["stdout"].write("child log\n")
            return child

        root = self.root.with_name("claude-k1-s0")
        root.mkdir(parents=True)
        (root / "run.log").write_text("previous log\n")
        with patch.object(run.subprocess, "Popen", side_effect=popen):
            code, out, err = self.invoke("--json", "run", "--seed", "0", "--detach")
        self.assertEqual(
            (code, json.loads(out), err),
            (0, {"name": root.name, "pid": 12345, "log": str(root / "run.log")}, ""),
        )
        self.assertEqual((root / "run.log").read_text(), "previous log\nchild log\n")
        child.wait.assert_called_once_with(timeout=2)
        self.suite.make_adapter.assert_not_called()
        self.assertFalse((root / "run.pid").exists())

    def test_detach_explicit_name_and_exact_text(self):
        child = Mock(pid=12345)
        child.wait.return_value = 0
        with patch.object(run.subprocess, "Popen", return_value=child) as popen:
            code, out, err = self.invoke("run", "--detach", "--name=chosen")
        log = self.root.with_name("chosen") / "run.log"
        self.assertEqual(
            (code, out, err),
            (
                0,
                f"Started run chosen in the background (pid 12345).\nFollow it: harness2 status chosen --watch\nStop it:   harness2 stop chosen\nLog:       {log}\n",
                "",
            ),
        )
        self.assertEqual(popen.call_args.args[0][-2:], ["run", "--name=chosen"])

    def test_detach_early_failure_reports_last_twenty_lines(self):
        child = Mock(pid=12345)
        child.wait.return_value = 7

        def popen(argv, **kwargs):
            kwargs["stdout"].write("".join(f"line {i}\n" for i in range(25)))
            return child

        with patch.object(run.subprocess, "Popen", side_effect=popen):
            code, out, err = self.invoke("--json", "run", "--detach")
        self.assertEqual((code, out), (7, ""))
        self.assertEqual(err.splitlines(), [f"line {i}" for i in range(5, 25)])

    def test_detach_dry_run_is_rejected(self):
        with patch.object(run.subprocess, "Popen") as popen:
            code, out, _ = self.invoke("--json", "run", "--detach", "--dry-run")
        self.assertEqual(
            (code, json.loads(out)),
            (1, {"error": "--detach starts a real run; drop --dry-run", "exit_code": 1}),
        )
        popen.assert_not_called()
        self.suite.make_adapter.assert_not_called()

    def test_already_running_refuses_both_start_modes(self):
        record = self.pid_record()
        for extra in ([], ["--detach"]):
            with self.subTest(extra=extra), patch.object(run.subprocess, "Popen") as popen:
                code, out, _ = self.invoke("--json", "run", *extra)
            self.assertEqual(
                (code, json.loads(out)),
                (
                    1,
                    {
                        "error": f"run claude-k1 is already running (pid {record['pid']})",
                        "exit_code": 1,
                    },
                ),
            )
            self.assertEqual(C.run_process(self.root), record)
            popen.assert_not_called()
        self.suite.make_adapter.assert_not_called()

    def test_run_process_live_stale_invalid_and_permission_denied(self):
        self.assertIsNone(C.run_process(self.root))
        record = self.pid_record()
        self.assertEqual(C.run_process(self.root), record)
        with patch.object(C.os, "kill", side_effect=PermissionError):
            self.assertEqual(C.run_process(self.root), record)
        with patch.object(C.os, "kill", side_effect=ProcessLookupError):
            self.assertIsNone(C.run_process(self.root))
        self.assertFalse((self.root / "run.pid").exists())
        for invalid in (
            "broken",
            "[]",
            "{}",
            json.dumps(record | {"pid": 0}),
            json.dumps(record | {"pid": True}),
            json.dumps(record | {"argv": [1]}),
        ):
            with self.subTest(invalid=invalid), patch.object(C.os, "kill") as kill:
                (self.root / "run.pid").write_text(invalid)
                self.assertIsNone(C.run_process(self.root))
                self.assertFalse((self.root / "run.pid").exists())
                kill.assert_not_called()

    def test_pid_lifecycle_success_failure_and_interrupt(self):
        argv = ["-C", str(self.project), "--json", "run"]
        for exception, expected in (
            (None, 0),
            (RuntimeError("crash"), 1),
            (KeyboardInterrupt(), 130),
            (events.Stopped("interrupted"), 130),
            (events.Stopped("max_calls"), 4),
        ):

            def sweep(*args, **kwargs):
                record = C.run_process(self.root)
                self.assertEqual(record["pid"], os.getpid())
                self.assertEqual(record["argv"], argv)
                datetime.fromisoformat(record["started"])
                self.assertTrue((self.root / "run.json").is_file())
                if exception is not None:
                    raise exception
                return {"domains": []}

            with (
                self.subTest(exception=exception),
                patch.object(run.schedule, "run_sweep", side_effect=sweep),
                patch.object(run, "Proposer", DemoProposer),
            ):
                code, out, _ = self.invoke("--json", "run")
            self.assertEqual(code, expected)
            self.assertFalse((self.root / "run.pid").exists())
            if expected in (1, 130):
                self.assertEqual(
                    json.loads(out),
                    {"error": "crash" if expected == 1 else "interrupted", "exit_code": expected},
                )
        code, _, _ = self.invoke("run", "--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "run.pid").exists())

    def test_json_sweep_error(self):
        with (
            patch.object(
                run.schedule, "run_sweep", return_value={"domains": [{"error": "failed"}]}
            ),
            patch.object(run, "Proposer", DemoProposer),
        ):
            code, out, _ = self.invoke("--json", "run")
        self.assertEqual((code, json.loads(out)), (1, {"error": "failed", "exit_code": 1}))

    def test_stop_signals_group_and_defaults_to_latest_run(self):
        self.pid_record()
        (self.root / "run.json").write_text("{}")
        with (
            patch("harness2.cli.stop.os.getpgid", return_value=9876),
            patch("harness2.cli.stop.os.killpg") as killpg,
            patch("harness2.cli.stop.os.kill") as kill,
        ):
            self.assertEqual(self.invoke("stop")[0], 0)
        killpg.assert_not_called()  # not a group leader: the launcher's group is left alone
        kill.assert_any_call(os.getpid(), signal.SIGINT)
        for json_flag in ([], ["--json"]):
            with (
                patch("harness2.cli.stop.os.getpgid", return_value=os.getpid()) as getpgid,
                patch("harness2.cli.stop.os.killpg") as killpg,
            ):
                code, out, err = self.invoke(*json_flag, "stop")
            self.assertEqual((code, err), (0, ""))
            getpgid.assert_called_once_with(os.getpid())
            killpg.assert_called_once_with(os.getpid(), signal.SIGINT)
            if json_flag:
                self.assertEqual(
                    json.loads(out), {"name": self.root.name, "pid": os.getpid(), "signalled": True}
                )
            else:
                self.assertEqual(
                    out,
                    f"Asked run claude-k1 (pid {os.getpid()}) to stop. It ends the calls in flight, then exits; run the same command again to resume.\n",
                )

    def test_stop_not_running_and_process_exit_race(self):
        self.root.mkdir(parents=True)
        (self.root / "run.json").write_text("{}")
        self.assertEqual(
            self.invoke("stop", self.root.name), (0, "Run claude-k1 is not running.\n", "")
        )
        code, out, err = self.invoke("--json", "stop")
        self.assertEqual(
            (code, json.loads(out), err),
            (0, {"name": self.root.name, "pid": None, "signalled": False}, ""),
        )
        self.pid_record()
        with patch("harness2.cli.stop.os.getpgid", side_effect=ProcessLookupError):
            self.assertEqual(self.invoke("stop"), (0, "Run claude-k1 is not running.\n", ""))

    def test_diff_json_and_unchanged_text(self):
        before = {"a": "old\n", "d": "removed\n"}
        after = {"a": "new\n", "b": "added\n"}
        adapter = DemoAdapter(self.project / "diff-fixture")
        changes = [["a", "M"], ["b", "A"], ["d", "D"]]
        diffs = {rel: _adopt.unified(rel, before, after) for rel, _ in changes}
        with (
            patch.object(_adopt, "open_run", return_value=(adapter, self.root)),
            patch.object(_adopt, "harness_for", return_value=("task-1", after)),
            patch.object(_adopt, "base_tree", return_value=before),
        ):
            for extra in ([], ["--stat"]):
                code, out, err = self.invoke("--json", "diff", *extra)
                self.assertEqual(
                    (code, json.loads(out), err),
                    (0, {"label": "task-1", "changes": changes, "diffs": diffs}, ""),
                )
            self.assertEqual(self.invoke("diff"), (0, "".join(diffs.values()), ""))
            self.assertEqual(
                self.invoke("diff", "--stat"),
                (0, str(diff_summary(adapter.substrate.surface(), before, after)) + "\n", ""),
            )


if __name__ == "__main__":
    unittest.main()
