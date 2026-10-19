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

"""Real CLI and tasks-suite workflow against one offline Claude executable."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from harness2.cli import _adopt, _common
from harness2.cli.main import build_parser, main
from harness2.core import events, runmark

FAKE_CLAUDE = r"""import json
import os
import sys
from pathlib import Path

if "--version" in sys.argv:
    print("fake claude 1.0")
    sys.exit(0)

prompt = sys.stdin.read()
assert prompt.strip(), "missing stdin prompt"
assert os.environ["ANTHROPIC_API_KEY"] == os.environ["E2E_API_KEY"]
cwd = Path.cwd()
output_format = sys.argv[sys.argv.index("--output-format") + 1]
model = sys.argv[sys.argv.index("--model") + 1]
context = cwd / "context"
if output_format == "stream-json":
    role = "executor"
elif (context / "candidates").is_dir():
    role = "selector"
elif (context / "adoption.json").is_file():
    role = "reflector"
else:
    role = "improver"
with open(os.environ["E2E_INVOCATIONS"], "a") as log:
    log.write(json.dumps({"role": role, "model": model, "cwd": str(cwd)}) + "\n")

if output_format == "stream-json":
    assert model == "x"
    assert not (cwd / ".harness2").exists(), "project state leaked into the workspace"
    assert (cwd / ".claude/skills/checks/SKILL.md").is_file()
    hooks = json.loads((cwd / ".claude/settings.json").read_text())["hooks"]
    assert "PreToolUse" in hooks
    if (cwd / "tiny_pkg/arithmetic.py").is_file():
        assert "Fix add" in prompt
        target = cwd / "tiny_pkg/arithmetic.py"
        target.write_text(target.read_text().replace("return a - b", "return a + b"))
        result = "Repaired add in tiny_pkg/arithmetic.py."
    else:
        assert "Uppercase" in prompt
        target = cwd / "output/result.txt"
        target.parent.mkdir(exist_ok=True)
        target.write_text((cwd / "message.txt").read_text().upper())
        result = "Saved output/result.txt."
    for event in (
        {"type": "system", "subtype": "init", "model": model, "cwd": str(cwd)},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "edit-1", "name": "Write",
             "input": {"file_path": str(target)}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "edit-1", "content": "File saved."}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": result}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": result, "total_cost_usd": 0.125},
    ):
        print(json.dumps(event))
else:
    assert output_format == "json" and model == "y"
    harness = context / "harness/CLAUDE.md"
    if harness.is_file():
        guidance = "Use the checks skill before finishing.\n"
        if guidance not in harness.read_text():
            harness.write_text(harness.read_text() + guidance)
    if role == "selector":
        chosen = sorted((context / "candidates").glob("*.md"))[0].stem
        text = "<why>The requested change is present.</why>\n```choice\n" + chosen + "\n```"
    elif role == "reflector":
        text = (
            "<edits>\ncomponent: CLAUDE.md\ncandidates: inv1\n"
            "change: named the checks skill\nobserved: the requested output was produced\n"
            "verdict: helped\n</edits>\n\n"
            "<experience>\ncue: When completing a task\n"
            "lesson: Use the checks skill before finishing.\n"
            "evidence: CLAUDE.md named the checks skill and the run produced its output.\n"
            "confidence: specific to this task\n</experience>"
        )
    else:
        text = "<notes>Name the checks skill in CLAUDE.md.</notes>"
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": text, "total_cost_usd": 0.01}))
"""


class EndToEndTests(unittest.TestCase):
    def test_tasks_cli_run_judge_adopt_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = root / "project"
            project.mkdir()
            binary_dir = root / "bin"
            binary_dir.mkdir()
            binary = binary_dir / "claude"
            binary.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE)
            binary.chmod(0o755)
            invocation_log = root / "invocations.jsonl"
            env = {
                "PATH": f"{binary_dir}{os.pathsep}{os.defpath}",
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "E2E_API_KEY": "offline-fixture",
                "E2E_INVOCATIONS": str(invocation_log),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            self.addCleanup(events.start, None)
            with patch.dict(os.environ, env, clear=True):
                package = project / "tiny_pkg"
                package.mkdir()
                (package / "__init__.py").write_text("")
                (package / "arithmetic.py").write_text("def add(a, b):\n    return a - b\n")
                tests = project / "tests"
                tests.mkdir()
                (tests / "test_arithmetic.py").write_text(
                    "import unittest\nfrom tiny_pkg.arithmetic import add\n\n"
                    "class ArithmeticTests(unittest.TestCase):\n"
                    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
                )
                base_instructions = "Read the source before editing.\n"
                (project / "CLAUDE.md").write_text(base_instructions)
                skill = project / ".claude/skills/checks/SKILL.md"
                skill.parent.mkdir(parents=True)
                skill_text = (
                    "---\nname: checks\ndescription: Run the project tests.\n---\n"
                    "Run python -m unittest discover -s tests.\n"
                )
                skill.write_text(skill_text)
                native_settings = {
                    "hooks": {
                        "PreToolUse": [
                            {"matcher": "Bash", "hooks": [{"type": "command", "command": "true"}]}
                        ]
                    },
                    "permissions": {"allow": ["Read"]},
                }
                native_path = project / ".claude/settings.json"
                native_path.write_text(json.dumps(native_settings))
                subprocess.run(["git", "init", "-q", str(project)], check=True)
                subprocess.run(["git", "add", "."], cwd=project, check=True)
                subprocess.run(
                    [
                        "git",
                        "-c",
                        "user.name=fixture",
                        "-c",
                        "user.email=fixture@localhost",
                        "-c",
                        "commit.gpgsign=false",
                        "commit",
                        "-qm",
                        "fixture",
                    ],
                    cwd=project,
                    check=True,
                )
                state = project / ".harness2"
                state.mkdir()
                (state / "config.toml").write_text(
                    '[executor]\nname = "claude"\nmodel = "anthropic/x"\n'
                    '[improver]\nchannel = "claude"\nmodel = "anthropic/y"\n'
                    '[providers.anthropic]\napi_key_env = "E2E_API_KEY"\n'
                )

                def invoke(*argv):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        code = main(["-C", str(project), *argv])
                    self.assertEqual(code, 0, f"{argv!r}\n{stdout.getvalue()}\n{stderr.getvalue()}")
                    return stdout.getvalue()

                project_check = shlex.join(
                    [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"]
                )
                invoke("task", "new", "fix-add", "--check", project_check)
                (state / "tasks/fix-add/task.md").write_text("Fix add so 2 plus 3 returns 5.\n")
                files_check = shlex.join(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        "from pathlib import Path; assert Path('output/result.txt').read_text() == 'HELLO\\n'",
                    ]
                )
                invoke("task", "new", "write-file", "--files", "--check", files_check)
                (state / "tasks/write-file/task.md").write_text(
                    "Uppercase message.txt and save output/result.txt.\n"
                )
                (state / "tasks/write-file/files/message.txt").write_text("hello\n")

                invoke("run", "--k", "1")
                invoke("run", "--k", "2", "--name", "k2")
                all_calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
                self.assertEqual(
                    Counter(call["role"] for call in all_calls),
                    {
                        "executor": 24,
                        "improver": 20,
                        "selector": 2,
                        "reflector": 4,
                    },
                )
                self.assertTrue(
                    all(
                        call["model"] == ("x" if call["role"] == "executor" else "y")
                        for call in all_calls
                    )
                )
                for name, k in (("claude-k1", 1), ("k2", 2)):
                    run_dir = state / "runs" / name
                    record = _common.load_run(run_dir)
                    self.assertEqual(record["schedule"]["k"], k)
                    self.assertEqual(
                        record["stream"], [["default", "fix-add"], ["default", "write-file"]]
                    )
                    event_rows = [
                        json.loads(line)
                        for line in (run_dir / "events.jsonl").read_text().splitlines()
                    ]
                    self.assertEqual(
                        (event_rows[0]["kind"], event_rows[-1]["status"]), ("run_start", "ok")
                    )
                    self.assertEqual(sum(row["kind"] == "rollout" for row in event_rows), 8 * k)
                    self.assertTrue(
                        all(
                            row["cost_usd"] == 0.125
                            for row in event_rows
                            if row["kind"] == "rollout"
                        )
                    )
                    self.assertTrue(
                        all(
                            row["cost_usd"] == 0.01
                            for row in event_rows
                            if row["kind"] == "session"
                        )
                    )
                run_rows = json.loads(invoke("--json", "runs", "ls"))
                self.assertEqual(
                    {
                        row["name"]: (row["done"], row["total"], row["spent_usd"])
                        for row in run_rows
                    },
                    {
                        "claude-k1": (2, 2, 1.09),
                        "k2": (2, 2, 2.17),
                    },
                )
                invoke("judge")
                rows = json.loads(invoke("--json", "report"))
                self.assertEqual({row["task_id"] for row in rows}, {"fix-add", "write-file"})
                self.assertTrue(
                    all(row["name"] == "k2" and row["graded"] and row["all_pass"] for row in rows)
                )
                self.assertTrue(all(row["raw"]["exit_code"] == 0 for row in rows))
                self.assertTrue(
                    any(
                        "Ran 1 test" in row["raw"]["output_tail"]
                        for row in rows
                        if row["task_id"] == "fix-add"
                    )
                )
                diff = invoke("diff")
                self.assertIn("--- a/CLAUDE.md", diff)
                self.assertIn("+Use the checks skill before finishing.", diff)

                args = build_parser().parse_args(["-C", str(project), "diff"])
                ctx = _common.Context(args, {})
                adapter, run_dir = _adopt.open_run(ctx, None)
                self.assertEqual(run_dir.name, "k2")
                chosen_task, adopted = _adopt.adopted_tree(adapter, run_dir, None, "parallel")
                self.assertEqual(chosen_task, "write-file")
                base = _adopt.base_tree(adapter)
                self.assertEqual(base["CLAUDE.md"], base_instructions)
                self.assertEqual(_adopt.changes(base, adopted), [("CLAUDE.md", "M")])
                for _, summary in _common.summaries(run_dir):
                    selected = summary["selections"]["agg"]
                    rollout = runmark.resolve_path(selected["chosen_run_dir"], run_dir)
                    self.assertTrue(adapter.has_judge(summary["task_id"]))
                    self.assertEqual(
                        json.loads((rollout / "judge.json").read_text())["n_passed"], 1
                    )
                    if summary["task_id"] == "fix-add":
                        deliverable = (rollout / "deliverable.patch").read_text()
                        self.assertIn("+    return a + b", deliverable)
                        self.assertNotIn("CLAUDE.md", deliverable)
                        self.assertNotIn(".claude/", deliverable)
                        self.assertNotIn(".harness2/", deliverable)
                    else:
                        self.assertEqual((rollout / "output/result.txt").read_text(), "HELLO\n")
                applied = _adopt.perform(ctx.settings, adapter, adopted, base=base, branch=None)
                self.assertEqual(applied, {"written": ["CLAUDE.md"], "deleted": []})
                self.assertEqual((project / "CLAUDE.md").read_text(), adopted["CLAUDE.md"])
                self.assertEqual(json.loads(native_path.read_text()), native_settings)
                self.assertEqual(skill.read_text(), skill_text)
                self.assertIn("return a - b", (package / "arithmetic.py").read_text())
                before_resume = invocation_log.read_bytes()
                invoke("run", "--k", "1")
                self.assertEqual(invocation_log.read_bytes(), before_resume)
                worktrees = subprocess.check_output(
                    ["git", "worktree", "list", "--porcelain"], cwd=project, text=True
                )
                self.assertEqual(worktrees.count("worktree "), 1)
                print("E2E: 2 task types; k=1/k=2; 50 calls; 2 graded; adopted; resume=0 calls")


if __name__ == "__main__":
    unittest.main()
