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

"""Offline command parsing, snapshots, adoption, and demo-backed CLI checks."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from harness2 import settings
from harness2.cli import _adopt, _final, completion, judge, main, run, runs
from harness2.cli import _common as C
from harness2.core import events
from harness2.core.judge import Scored
from harness2.core.proposer import Reply
from harness2.demo import DemoAdapter, DemoProposer
from harness2.substrate.claude import ClaudeSubstrate


class CliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        (self.project / ".harness2").mkdir(parents=True)
        self.env = patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}, clear=True
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(events.start, None)
        self.config_path = self.project / ".harness2/config.toml"
        self.config_path.write_text('[improver]\nmodel="openai/fake"\nchannel="opencode"\n')
        self.adapters = []

        def make_adapter(ctx):
            adapter = DemoAdapter(ctx.run_root / "fixture")
            adapter.spec = replace(adapter.spec, runs_root=ctx.run_root)
            adapter.has_judge = lambda task: task == "ledger-1"
            adapter.score = lambda path, task: {"n_passed": 1, "n_criteria": 1}
            self.adapters.append(adapter)
            return adapter

        self.suite = types.SimpleNamespace(make_adapter=make_adapter, check=lambda ctx: [])
        self.registry = patch("harness2.registry.load", return_value=self.suite)
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def invoke(self, *args, project=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = (["-C", str(self.project)] if project else []) + list(args)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def context(self, *args):
        parsed = main.build_parser().parse_args(["-C", str(self.project), *args])
        return C.Context(parsed, getattr(parsed._module, "FLAGS", {}))

    def execute(self, k=1, *extra):
        with (
            patch.object(run, "Proposer", DemoProposer),
            patch.object(_final, "Proposer", DemoProposer),
        ):
            return self.invoke("--json", "run", "--k", str(k), *extra)

    def test_all_command_arguments(self):
        commands = [
            [
                "run",
                "--suite",
                "tasks",
                "--executor",
                "claude",
                "--name",
                "x",
                "--k",
                "2",
                "--mode",
                "parallel,sequential",
                "--seed",
                "0",
                "--split",
                "train",
                "--split-file",
                "split.json",
                "--only-domains",
                "d",
                "--only-tasks",
                "a",
                "--max-tasks",
                "1",
                "--workers",
                "2",
                "--budget-usd",
                "1",
                "--max-calls",
                "20",
                "--selector",
                "human",
                "--steer",
                "@guide",
                "--dry-run",
            ],
            ["doctor", "--live"],
            ["setup", "tasks", "--flag"],
            ["judge", "x", "--bases"],
            ["report", "x", "y"],
            ["runs", "ls"],
            ["runs", "show", "x"],
            ["diff", "x", "--task", "a", "--mode", "sequential", "--stat"],
            ["task", "new", "a", "--files", "--domain", "d", "--check", "true"],
            ["task", "ls"],
            ["task", "import", "a.csv", "--force"],
            ["config", "show", "--origin"],
            ["config", "get", "run.k"],
            ["config", "set", "run.k", "2", "--global"],
            ["config", "path"],
            ["demo", "--output", "demo", "--k", "1", "--mode", "both"],
            ["bridge", "--model", "openai/model", "--host", "localhost", "--port", "1234"],
            ["completion", "zsh"],
            ["init"],
            ["status"],
            ["chat"],
            ["apply"],
        ]
        parser = main.build_parser()
        for argv in commands:
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertEqual(args.command, argv[0])
                self.assertTrue(callable(args._module.run))
        self.assertEqual(parser.parse_args(["run", "--bench", "jb"]).suite, "jb")
        self.assertEqual(parser.parse_args(["run", "--modes", "seq"]).modes, "seq")

    def test_context_overrides_origins_and_output(self):
        ctx = self.context(
            "-c", "run.k=4", "run", "--k", "2", "--selector", "human", "--workers", "3"
        )
        self.assertEqual(ctx.settings.get("run.k"), 2)
        self.assertEqual(ctx.settings.origin("run.k"), "flag --k")
        self.assertEqual(ctx.settings.role("selector")["channel"], "human")
        self.assertEqual(ctx.settings.get("run.workers"), 3)
        self.assertEqual(C.default_name(ctx.settings), "claude-k2")
        ctx = self.context("run", "--seed", "0")
        self.assertEqual(C.default_name(ctx.settings), "claude-k1-s0")
        code, stdout, _ = self.invoke("--json", "config", "show", "--origin")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["run.k"]["origin"], "default")
        with self.assertRaises(settings.ConfigError):
            self.context("-c", "oops", "config", "show").settings
        with self.assertRaises(settings.ConfigError):
            C.parse_modes("")
        self.assertEqual(C.parse_modes("seq,agg,parallel"), ("agg", "seq"))
        self.assertEqual(C.mode_label("seq"), "sequential")

    def test_lazy_project_and_errors(self):
        with patch.object(settings, "find_project", return_value=None):
            code, _, error = self.invoke("runs", "ls", project=False)
            self.assertEqual(code, 1)
            self.assertIn("not inside a harness2 project", error)
            self.assertEqual(
                self.invoke("--json", "config", "get", "run.k", project=False)[:2], (0, "1\n")
            )
        self.assertEqual(self.invoke("--quiet", "runs", "show", "missing")[2], "")
        self.assertIn("command: config", self.invoke("--verbose", "config", "path")[2])
        for command in ("status", "apply"):
            code, _, error = self.invoke(command, project=False)
            self.assertEqual(code, 1)
            self.assertIn("not inside a harness2 project", error)
        with self.assertRaises(settings.ConfigError):
            C.run_path(self.context("runs", "ls"), "../escape")
        for command in ("--help", "--version"):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main.main([command])
            self.assertEqual(raised.exception.code, 0)

    def test_run_identity_and_name_resolution(self):
        ctx = self.context("run")
        root = C.run_path(ctx, C.default_name(ctx.settings))
        plan = {"k": 1, "modes": "parallel", "seed": None}
        first = C.save_run(root, ctx.settings, [["d", "t"]], plan)
        self.assertNotIn("run.k", first["settings"])
        self.assertNotIn("judge.model", first["settings"])
        self.assertIn("executor.model", first["settings"])
        changed = self.context("run", "--workers", "9", "--k", "2")
        second = C.save_run(root, changed.settings, [["d", "t"]], {**plan, "k": 2})
        self.assertEqual(first["created"], second["created"])
        with self.assertRaisesRegex(settings.ConfigError, "executor.name.*claude.*codex"):
            C.save_run(
                root, self.context("run", "--executor", "codex").settings, [["d", "t"]], plan
            )
        with self.assertRaisesRegex(settings.ConfigError, "stream"):
            C.save_run(root, ctx.settings, [["d", "different"]], plan)
        recent = root.parent / "new"
        C.save_run(recent, ctx.settings, [["d", "t"]], plan)
        os.utime(root, ns=(1, 1))
        self.assertEqual(C.resolve_run(ctx, None), recent)
        self.assertEqual(C.resolve_run(ctx, root.name), root)
        (recent / "run.json").write_text("[]")
        with self.assertRaises(settings.ConfigError):
            C.load_run(recent)
        self.assertEqual(run._csv(" a, b, "), ("a", "b"))

    def test_snapshot_ignores_later_identity_settings(self):
        ctx = self.context("run")
        root = C.run_path(ctx, "snapshot")
        record = C.save_run(
            root, ctx.settings, [["d", "t"]], {"k": 1, "modes": "parallel", "seed": None}
        )
        self.config_path.write_text(
            '[executor]\nname="codex"\n[selector]\nchannel="human"\n[improver]\nmodel="openai/changed"\n'
        )
        rebuilt = C.run_settings(self.context("-c", "judge.model=openai/new", "judge"), record)
        self.assertEqual(C.identity(rebuilt), record["settings"])
        self.assertEqual(rebuilt.role("selector")["channel"], "opencode")
        self.assertEqual(rebuilt.role("judge")["model"], "openai/new")

    def test_dry_run_estimate_and_stream_filters(self):
        code, stdout, _ = self.invoke(
            "--json",
            "run",
            "--dry-run",
            "--k",
            "1",
            "--mode",
            "parallel,sequential",
            "--only-tasks",
            "ledger-2",
        )
        self.assertEqual(code, 0)
        data = json.loads(stdout)
        self.assertEqual(data["stream"], [["accounting", "ledger-2"]])
        self.assertEqual((data["estimate"]["rollouts"], data["estimate"]["sessions"]), (7, 10))
        self.assertEqual(data["estimate"]["per_task_per_mode"]["reflector"], 1)
        self.assertEqual(list((self.project / ".harness2/runs").rglob("run.json")), [])
        self.assertEqual(list((self.project / ".harness2/runs").rglob("events.jsonl")), [])
        code, stdout, _ = self.invoke("run", "--dry-run", "--k", "2", "--max-tasks", "1")
        self.assertEqual(code, 0)
        self.assertIn("would refine 1 task(s)", stdout)
        self.assertIn("accounting", stdout)
        self.assertIn("Model calls: 8 agent runs + 9", stdout)
        self.assertEqual(
            run._estimate(1, 2, ("agg",), {"spent_usd": 4, "calls": 2})["average_cost_per_call"], 2
        )
        self.suite.check = lambda ctx: ["fixture error"]
        code, _, error = self.invoke("run", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("fixture error", error)
        self.suite.check = lambda ctx: []
        self.suite.make_adapter = lambda ctx: types.SimpleNamespace(tasks=lambda split: [])
        self.assertEqual(self.invoke("run", "--dry-run")[0], 1)

    def test_demo_backed_run_judge_report_diff_for_each_scale(self):
        for k in (1, 2):
            with self.subTest(k=k):
                code, stdout, _ = self.execute(k, "--mode", "parallel,sequential")
                self.assertEqual(code, 0)
                data = json.loads(stdout)
                self.assertEqual(
                    data["totals"]["calls"], 2 * (k + 6 * k + 6 * k + 2 * (1 + int(k > 1))) + 2
                )
                name = f"claude-k{k}"
                root = self.project / ".harness2/runs" / name
                record = C.load_run(root)
                self.assertEqual(
                    record["stream"], [["accounting", "ledger-1"], ["accounting", "ledger-2"]]
                )
                self.assertEqual(record["schedule"]["k"], k)
                self.assertEqual(
                    record["settings"], C.identity(self.context("run", "--k", str(k)).settings)
                )
                stored_events = [
                    json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()
                ]
                self.assertEqual(stored_events[0]["kind"], "run_start")
                self.assertEqual(stored_events[-1]["status"], "ok")
                code, stdout, error = self.invoke("--json", "judge", name)
                self.assertEqual(code, 0)
                self.assertEqual(len(json.loads(stdout)), 2)
                self.assertEqual(error.count("no judge; skipped"), 1)
                judged = next((root / "adapt").glob("*/judged.json"))
                self.assertEqual(json.loads(judged.read_text())["judge_model"], "openai/fake")
                self.assertEqual(
                    self.invoke("-c", "judge.model=openai/different", "judge", name)[0], 1
                )
                code, stdout, _ = self.invoke("--json", "report", name)
                self.assertEqual((code, len(json.loads(stdout))), (0, 2))
                code, stdout, _ = self.invoke("--json", "diff", name)
                self.assertEqual(code, 0)
                diff = json.loads(stdout)
                self.assertEqual(diff["label"], "consolidated")
                self.assertIn("-total = sum", diff["diffs"]["scripts/summarize.py"])
                self.assertIn("+total = sum", diff["diffs"]["scripts/summarize.py"])
                code, stdout, _ = self.invoke(
                    "--json", "diff", name, "--task", "ledger-1", "--mode", "sequential", "--stat"
                )
                self.assertEqual(code, 0)
                self.assertIn(["scripts/summarize.py", "M"], json.loads(stdout)["changes"])
                self.assertEqual(self.invoke("diff", name, "--task", "missing")[0], 1)
                self.assertEqual(self.invoke("--json", "judge", name, "--bases")[0], 0)
                code, stdout, _ = self.invoke("--json", "runs", "show", name)
                self.assertEqual((code, json.loads(stdout)["done"]), (0, 2))
                with (
                    patch.object(run, "Proposer", DemoProposer),
                    patch.object(_final, "Proposer", DemoProposer),
                ):
                    code, stdout, _ = self.invoke(
                        "--json", "run", "--k", str(k), "--mode", "parallel,sequential"
                    )
                self.assertEqual((code, self.adapters[-1].execution_count), (0, 0))
        code, stdout, _ = self.invoke("--json", "runs", "ls")
        self.assertEqual((code, len(json.loads(stdout))), (0, 2))
        self.assertIn("claude-k1", self.invoke("runs", "ls")[1])
        code, stdout, _ = self.invoke("report", "claude-k1", "claude-k2")
        self.assertEqual(code, 0)
        self.assertIn("claude-k1", stdout)
        self.assertIn("claude-k2", stdout)
        self.assertIn("score 100.00%", stdout)

    def test_budget_resume_and_failure_exit_events(self):
        code, stdout, _ = self.execute(1, "--max-calls", "1")
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(stdout)["stopped"], "max_calls")
        root = self.project / ".harness2/runs/claude-k1"
        self.assertEqual(
            json.loads((root / "events.jsonl").read_text().splitlines()[-1])["status"], "stopped"
        )
        code, _, _ = self.execute(1, "--max-calls", "100")
        self.assertEqual(code, 0)
        for exception, expected, status in (
            (KeyboardInterrupt(), 130, "interrupted"),
            (events.Stopped("interrupted"), 130, "interrupted"),
            (events.Stopped("usage_limit"), 4, "stopped"),
            (RuntimeError("fixture crash"), 1, "error"),
        ):
            with (
                patch.object(run.schedule, "run_sweep", side_effect=exception),
                patch.object(run, "Proposer", DemoProposer),
                patch.object(run, "consolidate") as final,
            ):
                code, out, error = self.invoke("run")
            final.assert_not_called()
            self.assertEqual(code, expected)
            self.assertEqual(
                json.loads((root / "events.jsonl").read_text().splitlines()[-1])["status"], status
            )
            if isinstance(exception, events.Stopped) and str(exception) == "usage_limit":
                self.assertIn("reached the model provider's usage limit", out)
                self.assertIn("Run the same command once the limit resets to resume.", out)
            if expected == 130:
                self.assertIn("Run the same command to resume", out)
        for result, expected in (
            ({"domains": [{"failed": 1}]}, 3),
            ({"domains": [{"error": "dead"}]}, 1),
        ):
            with (
                patch.object(run.schedule, "run_sweep", return_value=result),
                patch.object(run, "Proposer", DemoProposer),
                patch.object(_final, "Proposer", DemoProposer),
            ):
                self.assertEqual(self.invoke("run")[0], expected)

    def test_human_selector_is_loaded_lazily(self):
        self.assertNotIn("harness2.cli.human", sys.modules)
        from harness2.core.spec import ChannelSpec

        original = self.suite.make_adapter

        def make(ctx):
            adapter = original(ctx)
            adapter.spec = replace(
                adapter.spec,
                channels={**adapter.spec.channels, "selector": ChannelSpec("human", "")},
            )
            return adapter

        self.suite.make_adapter = make
        with (
            patch.object(run.schedule, "run_sweep", return_value={"domains": [{"done": 2}]}),
            patch.object(run, "Proposer", DemoProposer),
        ):
            self.assertEqual(self.invoke("run", "--selector", "human")[0], 0)
        self.assertIn("harness2.cli.human", sys.modules)

    def test_json_stays_parseable_when_adapter_prints(self):
        self.execute()
        original = self.suite.make_adapter

        def make(ctx):
            print("adapter progress")
            adapter = original(ctx)

            def score(path, task):
                print("judge progress")
                return {"n_passed": 1, "n_criteria": 1}

            adapter.score = score
            return adapter

        self.suite.make_adapter = make
        code, stdout, stderr = self.invoke("--json", "judge")
        self.assertEqual((code, len(json.loads(stdout))), (0, 1))
        self.assertIn("judge progress", stderr)
        code, stdout, stderr = self.invoke("--json", "--quiet", "judge")
        self.assertEqual((code, len(json.loads(stdout)), stderr), (0, 1, ""))

    def test_config_task_setup_and_json(self):
        self.assertEqual(self.invoke("--json", "config", "set", "run.k", "3")[0], 0)
        self.assertEqual(self.invoke("--json", "config", "get", "run.k")[:2], (0, "3\n"))
        self.assertEqual(self.invoke("config", "set", "run.k", "0")[0], 1)
        self.assertEqual(self.invoke("config", "set", "run.k", "4", "--global")[0], 0)
        self.assertTrue(settings.global_path().is_file())
        rows = [
            {"id": "x", "domain": "d", "split": "train", "workspace": "files", "has_judge": True}
        ]
        fake = types.ModuleType("harness2.suite")
        fake.new_task = Mock(return_value=self.project / "task.md")
        fake.list_tasks = Mock(return_value=rows)
        fake.import_tasks = Mock(return_value=["x"])
        with (
            patch.dict(sys.modules, {"harness2.suite": fake}),
            patch("harness2.suite", fake, create=True),
        ):
            self.assertEqual(
                self.invoke(
                    "--json", "task", "new", "x", "--files", "--domain", "d", "--check", "true"
                )[0],
                0,
            )
            fake.new_task.assert_called_once_with(
                self.project / ".harness2/tasks", "x", files=True, domain="d", check="true"
            )
            code, stdout, _ = self.invoke("--json", "task", "ls")
            self.assertEqual((code, json.loads(stdout)), (0, rows))
            self.assertRegex(self.invoke("task", "ls")[1], r"x +d +train +files +yes")
            self.assertEqual(self.invoke("--json", "task", "import", "tasks.csv", "--force")[0], 0)
            fake.import_tasks.assert_called_once_with(
                self.project / ".harness2/tasks", Path("tasks.csv"), force=True
            )
        self.assertEqual(self.invoke("setup", "demo")[0], 1)
        self.suite.setup = Mock(return_value=7)
        self.assertEqual(self.invoke("setup", "demo", "--example")[0], 7)
        self.suite.setup.assert_called_once_with(["--example"])

    def test_doctor_fake_binary_and_live_deduplication(self):
        binary = self.root / "bin/opencode"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nprintf 'fixture 1.0\\n'\n")
        binary.chmod(0o755)
        os.environ["PATH"] = str(binary.parent)
        os.environ["OPENAI_API_KEY"] = "fixture"
        self.config_path.write_text(
            '[executor]\nname="opencode"\nmodel="openai/fake"\n[improver]\nchannel="opencode"\nmodel="openai/fake"\n'
        )
        with patch(
            "harness2.cli.doctor.Proposer.call", return_value=Reply("doctor", "ok", True)
        ) as call:
            code, stdout, _ = self.invoke("--json", "doctor", "--live")
        self.assertEqual(code, 0)
        self.assertEqual(call.call_count, 1)
        rows = json.loads(stdout)
        self.assertEqual(sum(row["check"].endswith(" CLI") for row in rows), 1)
        self.assertTrue(any("fixture 1.0" in row["detail"] for row in rows))
        os.environ.pop("OPENAI_API_KEY")
        binary.unlink()
        code, stdout, _ = self.invoke("--json", "doctor")
        self.assertEqual(code, 1)
        self.assertTrue(
            any(not row["ok"] and row["check"].endswith(" CLI") for row in json.loads(stdout))
        )
        self.config_path.write_text('[improver]\nmodel="invalid"\n')
        self.assertEqual(self.invoke("doctor")[0], 1)

    def test_completion_aliases_and_module_entry(self):
        for shell in ("bash", "zsh"):
            script = completion.generate(main.build_parser(), shell)
            for command in main.COMMANDS:
                self.assertIn(":" + command, script)
            self.assertIn("--only-tasks", script)
            binary = shutil.which(shell)
            if binary is None:  # e.g. no zsh on a Linux CI runner: only the text is checked
                continue
            result = subprocess.run([binary, "-n"], input=script, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            if shell == "bash":
                probe = (
                    script
                    + '\nCOMP_WORDS=(harness2 run --on); COMP_CWORD=2; _harness2_complete; printf "%s\\n" "${COMPREPLY[@]}"'
                )
            else:
                probe = (
                    'compdef() { :; }; compadd() { print -l -- "$@"; };\n'
                    + script
                    + "\nwords=(harness2 run --on); CURRENT=3; _harness2_complete"
                )
            result = subprocess.run([binary], input=probe, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--only-tasks", result.stdout)
        for executable, command in main.ALIASES.items():
            with (
                patch.object(sys, "argv", [executable]),
                patch.object(main, "build_parser", wraps=main.build_parser),
            ):
                with (
                    contextlib.redirect_stdout(io.StringIO()) as stdout,
                    self.assertRaises(SystemExit),
                ):
                    main.main(["--help"])
                self.assertIn("harness2 " + command, stdout.getvalue())
        with patch.object(sys, "argv", ["harness2-check"]):
            self.assertEqual(self.invoke("--json", "--live")[0], 1)
        result = subprocess.run(
            [sys.executable, "-B", "-m", "harness2", "--version"], capture_output=True, text=True
        )
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "0.1.0"))

    def test_judge_legacy_cache_and_merge(self):
        good = Scored("t", "agg", 1, "agg1", "id", False, n_passed=1, n_criteria=1)
        bad = Scored("t", "agg", 1, "agg1", "id", False, error="judge failed")
        self.assertEqual(judge._merge([good], [bad]), [good])
        changed = replace(bad, run_id="new")
        self.assertEqual(judge._merge([good], [changed]), [changed])
        path = self.root / "judged.json"
        self.assertEqual(judge._load_rows(path), [])
        row = good.__dict__ | {"graded": True, "frac": 1.0}
        row["arm"] = row.pop("mode")
        path.write_text(json.dumps([row]))
        self.assertEqual(judge._load_rows(path), [good])
        judge._judge_model(path, "new")
        path.write_text(json.dumps({"judge_model": "old", "rows": [row]}))
        with self.assertRaises(settings.ConfigError):
            judge._judge_model(path, "new")
        path.write_text("broken")
        with self.assertRaises(ValueError):
            judge._load_rows(path)
        with self.assertRaises(settings.ConfigError):
            judge._judge_model(path, "new")
        path = self.root / "ns/t/summary.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"selections": {"agg": row}}))
        self.assertEqual(judge.collect_selections(path.parent.parent, ("agg",))[0]["mode"], "agg")
        path.write_text("broken")
        with self.assertRaises(settings.ConfigError):
            judge.collect_selections(path.parent.parent, ("agg",))

    def test_adoption_changes_diff_and_native_export(self):
        before = {"a": "old", "d": "removed\n", "same": "same"}
        after = {"a": "new", "b": "added\n", "same": "same"}
        self.assertEqual(_adopt.changes(before, after), [("a", "M"), ("b", "A"), ("d", "D")])
        self.assertIn("No newline at end of file", _adopt.unified("a", before, after))
        self.assertIn("--- /dev/null", _adopt.unified("b", before, after))
        self.assertIn("+++ /dev/null", _adopt.unified("d", before, after))
        adapter = types.SimpleNamespace(substrate=ClaudeSubstrate())
        config = self.context("config", "show").settings
        (self.project / "CLAUDE.md").write_text("old\n")
        base = _adopt.project_tree(adapter, config)
        self.assertEqual(base, {"CLAUDE.md": "old\n"})
        self.assertEqual(_adopt.dirty_paths(config, adapter), [])

        def git(*args):
            return subprocess.run(
                ["/usr/bin/git", "-C", str(self.project), *args],
                check=True,
                capture_output=True,
                text=True,
            )

        git("init", "-q")
        git("add", "CLAUDE.md")
        git("-c", "user.name=test", "-c", "user.email=test@example.test", "commit", "-qm", "base")
        (self.project / "unrelated.txt").write_text("keep")
        (self.project / "CLAUDE.md").write_text("dirty")
        (self.project / ".claude/scripts").mkdir(parents=True)
        (self.project / ".claude/scripts/new.py").write_text("pass\n")
        self.assertEqual(
            _adopt.dirty_paths(config, adapter), [".claude/scripts/new.py", "CLAUDE.md"]
        )
        result = _adopt.perform(
            config, adapter, {"CLAUDE.md": "adopted\n"}, base=base, branch="adopt"
        )
        self.assertEqual(result, {"written": ["CLAUDE.md"], "deleted": []})
        self.assertEqual(git("branch", "--show-current").stdout.strip(), "adopt")
        self.assertEqual((self.project / "unrelated.txt").read_text(), "keep")
        with self.assertRaises(settings.ConfigError):
            _adopt.perform(config, adapter, {}, base=base, branch="adopt")
        self.assertEqual(
            _adopt.perform(config, adapter, {}, base=base, branch=None)["deleted"], ["CLAUDE.md"]
        )

    def test_missing_and_corrupt_state(self):
        self.assertEqual(self.invoke("report")[0], 1)
        self.assertEqual(self.invoke("--json", "runs", "ls")[:2], (0, "[]\n"))
        self.execute()
        ctx = self.context("runs", "ls")
        adapter, root = _adopt.open_run(ctx, None)
        data = C.load_run(root)
        for _, task in data["stream"]:
            path = root / "adapt/demo-oc-agg1" / task / "summary.json"
            summary = json.loads(path.read_text())
            summary["selections"]["agg"]["ok"] = False
            path.write_text(json.dumps(summary))
        with self.assertRaises(settings.ConfigError):
            _adopt.adopted_tree(adapter, root, None, "parallel")
        (root / "events.jsonl").write_text("broken")
        with self.assertRaises(settings.ConfigError):
            C.event_totals(root)
        with self.assertRaises(settings.ConfigError):
            runs.describe(root)


if __name__ == "__main__":
    unittest.main()
