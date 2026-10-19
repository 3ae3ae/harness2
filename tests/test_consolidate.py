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

"""Offline consolidation, adoption, and evaluation through a fake Claude CLI."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harness2.cli import _adopt, _common, _final, eval, run
from harness2.cli.main import COMMANDS, build_parser, main
from harness2.core import events
from harness2.settings import ConfigError
from harness2.suite import GenericAdapter

FAKE_CLAUDE = r"""import json
import os
import sys
from pathlib import Path

if "--version" in sys.argv:
    print("fake claude 1.0")
    sys.exit(0)

prompt = sys.stdin.read()
assert prompt.strip()
assert os.environ["ANTHROPIC_API_KEY"] == "offline-fixture"
cwd = Path.cwd()
context = cwd / "context"
output_format = sys.argv[sys.argv.index("--output-format") + 1]
if output_format == "stream-json":
    output = cwd / "output/result.txt"
    output.parent.mkdir(exist_ok=True)
    output.write_text("pass" if "Use shared procedure." in (cwd / "CLAUDE.md").read_text() else "fail")
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "Saved the result.", "total_cost_usd": 0.125}))
    sys.exit(0)

harness = context / "harness/CLAUDE.md"
if "You are consolidating an autonomous agent" in prompt:
    counter = Path(os.environ["WC_CALLS"])
    call = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(call))
    task_dirs = sorted((context / "tasks").iterdir())
    index = (context / "INDEX.md").read_text().splitlines()
    assert len(index) == len(task_dirs)
    assert f"run on {len(task_dirs)} task(s)" in prompt
    for task in task_dirs:
        assert (task / "task.md").read_text() == f"Complete {task.name}.\n"
        assert "Task note." in (task / "harness/CLAUDE.md").read_text()
        assert (task / "EXPERIENCE.md").is_file() == any(
            line.startswith(f"- {task.name} ") and line.endswith("lesson: yes") for line in index)
    assert all("(ops/review): adopted harness differs from the start; lesson:" in line for line in index)
    assert not list(context.rglob("rubric.md"))
    behavior = os.environ.get("WC_BEHAVIOR", "retry")
    invalid = context / "harness/.claude/scripts/broken.py"
    if behavior == "missing":
        import shutil
        shutil.rmtree(context / "harness")
    elif behavior == "invalid" or (behavior == "retry" and call == 1):
        invalid.parent.mkdir(parents=True, exist_ok=True)
        invalid.write_text("def broken(")
    else:
        if behavior == "retry" and call == 2:
            assert "# YOUR PREVIOUS ATTEMPT WAS REJECTED" in prompt
            assert "broken.py" in prompt
            assert invalid.is_file()
        invalid.unlink(missing_ok=True)
        harness.write_text("Read sources.\n" if behavior == "unchanged" else "Use shared procedure.\n")
    text = "<notes>a-one and b-two support the shared procedure.</notes>"
elif (context / "adoption.json").is_file():
    text = ("<edits>\ncomponent: CLAUDE.md\ncandidates: inv1\n"
            "change: added a procedure\nobserved: the output was produced\nverdict: helped\n</edits>\n"
            "<experience>\ncue: When doing project work\nlesson: Use a shared procedure.\n"
            "evidence: A task note accompanied the completed output.\n"
            "confidence: specific to this task\n</experience>")
else:
    harness.write_text(harness.read_text() + "Task note.\n")
    text = "<notes>Added a task note.</notes>"
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": text, "total_cost_usd": 0.01}))
"""


class ConsolidateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        self.state = self.project / ".harness2"
        self.state.mkdir(parents=True)
        (self.project / "CLAUDE.md").write_text("Read sources.\n")
        (self.state / "config.toml").write_text(
            '[executor]\nname = "claude"\nmodel = "anthropic/x"\n'
            '[improver]\nchannel = "claude"\nmodel = "anthropic/y"\n'
            '[providers.anthropic]\napi_key_env = "WC_KEY"\n'
        )
        binaries = self.root / "bin"
        binaries.mkdir()
        binary = binaries / "claude"
        binary.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE)
        binary.chmod(0o755)
        self.calls = self.root / "consolidate-calls"
        environment = patch.dict(
            os.environ,
            {
                "PATH": f"{binaries}{os.pathsep}{os.defpath}",
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "WC_KEY": "offline-fixture",
                "WC_CALLS": str(self.calls),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(events.start, None)
        for task, split in (("a-one", "train"), ("b-two", "test")):
            self.new_task(task, split=split)
        self.run_dir = self.state / "runs/trial"

    def invoke(self, *argv, code=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(["-C", str(self.project), *argv])
        self.assertEqual(result, code, f"{argv!r}\n{stdout.getvalue()}\n{stderr.getvalue()}")
        return stdout.getvalue(), stderr.getvalue()

    def new_task(self, task, *, split="train", judge=True):
        check = shlex.join(
            [
                sys.executable,
                "-B",
                "-c",
                "from pathlib import Path; assert Path('output/result.txt').read_text() == 'pass'",
            ]
        )
        self.invoke(
            "task",
            "new",
            task,
            "--files",
            "--domain",
            "ops/review",
            *(["--check", check] if judge else []),
        )
        directory = self.state / "tasks" / task
        (directory / "task.md").write_text(f"Complete {task}.\n")
        with (directory / "task.toml").open("a") as stream:
            stream.write(f'split = "{split}"\n')

    def execute(self, *argv, code=0):
        return self.invoke("run", "--name", "trial", *argv, code=code)

    def adapter(self):
        args = build_parser().parse_args(["-C", str(self.project), "diff", "trial"])
        return _adopt.open_run(_common.Context(args, {}), "trial")[0]

    def test_two_tasks_retry_adoption_eval_and_json(self):
        text, _ = self.execute()
        before = _common.event_totals(self.run_dir)
        self.assertEqual(self.calls.read_text(), "2")
        path = self.run_dir / "consolidated/parallel/harness"
        self.assertEqual((path / "CLAUDE.md").read_text(), "Use shared procedure.\n")
        self.assertIn(f"Final harness (parallel): {path}", text)
        self.assertIn(
            "Next: harness2 diff trial shows it; harness2 apply trial writes it into the project; "
            "harness2 eval trial measures it first.",
            text,
        )
        meta = json.loads((path.parent / "consolidation.json").read_text())
        self.assertEqual(meta["tasks"], ["a-one", "b-two"])
        self.assertEqual((meta["model"], meta["cost_usd"]), ("anthropic/y", 0.02))
        self.assertTrue(meta["created"])
        self.assertEqual(
            (path.parent / "notes.md").read_text(),
            "a-one and b-two support the shared procedure.\n",
        )
        data = json.loads(self.invoke("--json", "run", "--name", "trial")[0])
        self.assertEqual(set(data), {"result", "totals", "final"})
        self.assertEqual(data["final"], {"parallel": str(path)})
        self.assertEqual(data["totals"]["calls"], before["calls"])
        self.assertEqual(data["totals"]["spent_usd"], before["spent_usd"])
        self.assertEqual(self.calls.read_text(), "2")
        self.assertTrue(list((self.run_dir / "consolidated/_audit").glob("*.response.md")))
        consolidated = json.loads(self.invoke("--json", "diff", "trial")[0])
        self.assertEqual(consolidated["label"], "consolidated")
        self.assertIn("+Use shared procedure.", consolidated["diffs"]["CLAUDE.md"])
        task = json.loads(self.invoke("--json", "diff", "trial", "--task", "a-one")[0])
        self.assertEqual(task["label"], "a-one")
        self.assertIn("+Task note.", task["diffs"]["CLAUDE.md"])
        applied, _ = self.invoke("apply", "trial", "--dry-run", "--task", "a-one")
        self.assertIn("Harness adopted for a-one (run trial, parallel)", applied)
        applied, _ = self.invoke("apply", "trial", "--yes")
        self.assertIn("Final harness of run trial (parallel)", applied)
        self.assertEqual((self.project / "CLAUDE.md").read_text(), "Use shared procedure.\n")
        text, _ = self.invoke("eval", "trial", "--repeat", "2")
        self.assertIn(
            "Comparing the final harness with the starting one on 2 task(s), 2 run(s) each.",
            text,
        )
        self.assertRegex(text, r"TASK +START +FINAL\n +a-one +0% +100%\n +b-two +0% +100%")
        self.assertRegex(text, r"mean +0% +100%  \(\+100 points\)")
        self.assertIn(f"Saved to {self.run_dir / 'eval/parallel/results.json'}", text)
        results = json.loads((self.run_dir / "eval/parallel/results.json").read_text())
        self.assertEqual(results["mean"], {"start": 0.0, "consolidated": 1.0})
        for row in results["tasks"].values():
            self.assertEqual(
                row["start"], {"runs": [{"n_passed": 0, "n_criteria": 1}] * 2, "mean": 0.0}
            )
            self.assertEqual(
                row["consolidated"], {"runs": [{"n_passed": 1, "n_criteria": 1}] * 2, "mean": 1.0}
            )
        event_rows = [
            json.loads(line) for line in (self.run_dir / "events.jsonl").read_text().splitlines()
        ]
        eval_rows = [row for row in event_rows if row.get("run_id", "").startswith("eval/")]
        self.assertEqual(len(eval_rows), 8)
        self.assertEqual({row["cost_usd"] for row in eval_rows}, {0.125})
        self.assertAlmostEqual(
            _common.event_totals(self.run_dir)["spent_usd"], before["spent_usd"] + 1.0
        )
        self.assertEqual(
            {row["run_id"] for row in eval_rows},
            {
                f"eval/{name}/{task}/{i}"
                for task in ("a-one", "b-two")
                for name in ("start", "consolidated")
                for i in range(2)
            },
        )

    def test_eval_filters_skipped_failed_grades_and_limits(self):
        self.new_task("c-no-judge", judge=False)
        self.execute()
        text, _ = self.invoke("eval", "trial")
        self.assertIn("Skipped 1 task(s) with no check or rubric: c-no-judge", text)
        data = json.loads(
            self.invoke(
                "--json",
                "eval",
                "trial",
                "--split",
                "test",
                "--only-tasks",
                "b-two",
                "--repeat",
                "2",
            )[0]
        )
        self.assertEqual(list(data["tasks"]), ["b-two"])
        self.assertEqual(data["skipped"], [])
        self.assertEqual(data["repeat"], 2)
        self.assertEqual(json.loads(Path(data["path"]).read_text()), data)
        with patch.object(GenericAdapter, "score", return_value=None):
            text, _ = self.invoke("eval", "trial", "--only-tasks", "a-one", code=3)
        self.assertRegex(text, r"a-one +- +-")
        self.assertRegex(text, r"mean +- +-  \(- points\)")
        data = json.loads((self.run_dir / "eval/parallel/results.json").read_text())
        self.assertEqual(data["tasks"]["a-one"]["start"], {"runs": [None], "mean": None})
        text, _ = self.invoke("-c", "run.max_calls=1", "eval", "trial", code=4)
        self.assertIn("Run trial stopped: reached --max-calls 1", text)
        with patch.object(GenericAdapter, "execute", side_effect=events.Stopped("usage_limit")):
            self.invoke("eval", "trial", code=4)
        _, err = self.invoke("eval", "trial", "--repeat", "0", code=1)
        self.assertIn("--repeat must be a positive integer", err)
        _, err = self.invoke("eval", "trial", "--only-tasks", "missing", code=1)
        self.assertIn("unknown task id(s)", err)
        data = json.loads(self.invoke("--json", "eval", "trial", "--only-tasks", "c-no-judge")[0])
        self.assertEqual(data["tasks"], {})
        self.assertEqual(data["mean"], {"start": None, "consolidated": None})
        self.assertEqual(data["skipped"], ["c-no-judge"])

    def test_modes_and_unchanged_harness(self):
        text, _ = self.execute("--mode", "parallel,sequential")
        self.assertEqual(self.calls.read_text(), "3")
        adapter = self.adapter()
        for mode in ("parallel", "sequential"):
            path = self.run_dir / "consolidated" / mode / "harness"
            self.assertIn(f"Final harness ({mode}): {path}", text)
            self.assertEqual(
                _adopt.harness_for(adapter, self.run_dir, None, mode)[0], "consolidated"
            )
        self.assertEqual(
            _adopt.harness_for(adapter, self.run_dir, "a-one", "sequential")[0], "a-one"
        )
        with patch.dict(os.environ, {"WC_BEHAVIOR": "unchanged"}):
            self.invoke("run", "--name", "unchanged")
        self.assertEqual(self.invoke("diff", "unchanged")[0], "\n")

    def test_validation_failure_keeps_exit_code_and_can_retry(self):
        with patch.dict(os.environ, {"WC_BEHAVIOR": "invalid"}):
            text, err = self.execute()
        self.assertIn("Could not build the final harness (parallel): ", err)
        self.assertIn("broken.py", err)
        self.assertTrue(err.rstrip().endswith(". Run the same command again to retry."))
        self.assertNotIn("Final harness (parallel):", text)
        self.assertFalse((self.run_dir / "consolidated/parallel/harness").exists())
        self.assertEqual(self.calls.read_text(), "2")
        _, err = self.invoke("eval", "trial", code=1)
        self.assertIn("this run has no final harness yet; run `harness2 run` to finish it", err)
        with patch.dict(os.environ, {"WC_BEHAVIOR": "valid"}):
            self.execute()
        self.assertEqual(self.calls.read_text(), "3")
        path = self.run_dir / "consolidated/parallel/harness/CLAUDE.md"
        self.assertEqual(path.read_text(), "Use shared procedure.\n")
        for failure, expected in (
            ("failure", 0),
            ("failure", 3),
            ("failure", 1),
            (events.Stopped("usage_limit"), 0),
        ):
            error = failure if isinstance(failure, Exception) else ConfigError(failure)
            result = {
                "domains": [
                    {"failed": int(expected == 3), **({"error": "dead"} if expected == 1 else {})}
                ]
            }
            with (
                patch.object(run.schedule, "run_sweep", return_value=result),
                patch.object(run, "consolidate", side_effect=error),
            ):
                text, err = self.execute(code=expected)
            self.assertEqual(
                err,
                f"Could not build the final harness (parallel): {error}. "
                "Run the same command again to retry.\n",
            )
            self.assertEqual(path.read_text(), "Use shared procedure.\n")

    def test_missing_workspace_failure_keeps_exit_code(self):
        with patch.dict(os.environ, {"WC_BEHAVIOR": "missing"}):
            _, err = self.execute()
        self.assertIn(
            "Could not build the final harness (parallel): "
            "the harness directory did not come back from the session. "
            "Run the same command again to retry.",
            err,
        )
        self.assertEqual(self.calls.read_text(), "2")

    def test_context_stream_order_optional_lessons_and_errors(self):
        self.execute()
        adapter = self.adapter()
        record = _common.load_run(self.run_dir)
        plan = record["schedule"]
        lesson = (
            _common.library_root_for(adapter, plan["k"], seed=plan.get("seed"))
            / "agg/ops__review/harnesses/b-two/EXPERIENCE.md"
        )
        lesson.unlink()
        record["stream"].reverse()
        dest = self.root / "context"
        base = _adopt.base_tree(adapter)
        task_path, row = next(
            (path, row)
            for path, row in _common.summaries(self.run_dir)
            if row["task_id"] == "b-two"
        )
        row["selections"]["agg"]["chosen_tree_is_base"] = True
        task_path.write_text(json.dumps(row))
        tasks = _final._context(adapter, self.run_dir, record, "parallel", dest, base)
        self.assertEqual(tasks, ["b-two", "a-one"])
        self.assertEqual(
            (dest / "INDEX.md").read_text(),
            "- b-two (ops/review): adopted harness same as the start; lesson: no\n"
            "- a-one (ops/review): adopted harness differs from the start; lesson: yes\n",
        )
        self.assertEqual((dest / "tasks/b-two/task.md").read_text(), "Complete b-two.\n")
        self.assertFalse((dest / "tasks/b-two/EXPERIENCE.md").exists())
        for path, row in _common.summaries(self.run_dir):
            row["selections"]["agg"]["ok"] = False
            path.write_text(json.dumps(row))
        with self.assertRaisesRegex(ConfigError, "no successful selection"):
            _final._context(adapter, self.run_dir, record, "parallel", self.root / "empty", base)
        with (
            patch.object(run.schedule, "run_sweep", return_value={"domains": [{"failed": 2}]}),
            patch.object(run, "consolidate") as final,
        ):
            data = json.loads(self.invoke("--json", "run", "--name", "trial", code=3)[0])
        final.assert_not_called()
        self.assertEqual(data["final"], {})

    def test_rollout_grades_and_optional_cost(self):
        adapter = SimpleNamespace(execute=Mock(return_value=self.root), score=Mock())
        for grade in (
            None,
            {},
            {"n_passed": 1, "n_criteria": 0},
            {"n_passed": True, "n_criteria": 1},
            {"n_passed": 2, "n_criteria": 1},
        ):
            adapter.score.return_value = grade
            self.assertIsNone(eval._rollout(adapter, "task", None, "eval/start/task/0"))
        adapter.score.side_effect = ValueError("failed judge")
        self.assertIsNone(eval._rollout(adapter, "task", None, "eval/start/task/0"))
        adapter.score.side_effect = events.Stopped("usage_limit")
        with self.assertRaisesRegex(events.Stopped, "usage_limit"):
            eval._rollout(adapter, "task", None, "eval/start/task/0")
        self.assertEqual(eval._mean([0.0, None, 1.0]), 0.5)
        self.assertIsNone(eval._mean([None]))
        data = {
            "tasks": {},
            "repeat": 1,
            "skipped": [],
            "path": "results.json",
            "mean": {"start": 1.0, "consolidated": 0.0},
        }
        self.assertRegex(eval._text(data), r"mean +100% +0%  \(-100 points\)")

    def test_command_order_and_help(self):
        i = COMMANDS.index("diff")
        self.assertEqual(COMMANDS[i + 1], "eval")
        self.assertNotIn("consolidate", COMMANDS)
        parser = build_parser()
        sub = parser._subparsers._group_actions[0].choices["eval"]
        for action in sub._actions:
            self.assertTrue(action.help)
            if action.dest != "help":
                self.assertEqual(action.help, action.help.lower())
                self.assertFalse(action.help.endswith("."))


if __name__ == "__main__":
    unittest.main()
