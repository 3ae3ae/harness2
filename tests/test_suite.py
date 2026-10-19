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

"""The generic suite against fake CLIs, local git projects, and the real scheduler."""

from __future__ import annotations

import csv
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from harness2 import registry, suite, workspace
from harness2.core import events, runmark, schedule, tree
from harness2.core.proposer import Proposer
from harness2.core.spec import BenchmarkAdapter
from harness2.settings import ConfigError, load

FAKE_CLI = r"""import json
import os
import sys
import time
from pathlib import Path

if "--version" in sys.argv:
    print("fake cli 1.0")
    sys.exit(0)
cwd = Path.cwd()
prompt = sys.stdin.read()
record = {"cwd": str(cwd), "argv": sys.argv[1:], "prompt": prompt,
          "claude_home": os.environ.get("CLAUDE_CONFIG_DIR"),
          "codex_home": os.environ.get("CODEX_HOME"),
          "harness": {name: (cwd / name).read_text() for name in ("CLAUDE.md", "AGENTS.md")
                      if (cwd / name).is_file()}}
if record["codex_home"]:
    record["config"] = (Path(record["codex_home"]) / "config.toml").read_text()
with open(os.environ["FAKE_RECORD"], "a") as stream:
    stream.write(json.dumps(record) + "\n")
if "--output-format" in sys.argv and sys.argv[sys.argv.index("--output-format") + 1] == "json":
    context = cwd / "context"
    if (context / "rubric.md").is_file():
        assert (context / "task.md").is_file()
        assert (context / "deliverables/final.md").is_file()
        text = os.environ.get("FAKE_JUDGE", '{"n_passed": 2, "n_criteria": 3, "notes": "one missing"}')
    elif (context / "candidates").is_dir():
        chosen = sorted((context / "candidates").glob("*.md"))[0].stem
        text = "<why>Complete change.</why>\n```choice\n" + chosen + "\n```"
    elif (context / "adoption.json").is_file():
        text = "NO_ENTRY"
    else:
        harness = context / "harness/CLAUDE.md"
        harness.write_text(harness.read_text() + "Run the project tests.\n")
        text = "<notes>Use the project's tests.</notes>"
    print(json.dumps({"type": "result", "subtype": "success", "result": text,
                      "total_cost_usd": 0.05}))
    sys.exit(0)
if Path(sys.argv[0]).name == "codex" and (cwd / "context/rubric.md").is_file():
    print(json.dumps({"type": "item.completed", "item": {
        "id": "judge", "type": "agent_message", "text": os.environ["FAKE_JUDGE"]}}))
    sys.exit(0)
assert not (cwd / ".harness2").exists(), "project state leaked"
assert not (cwd / "check").exists(), "checks leaked"
assert not (cwd / "rubric.md").exists(), "rubric leaked"
(cwd / "code.txt").write_text("after\n")
for name in ("CLAUDE.md", "AGENTS.md"):
    if (cwd / name).is_file():
        (cwd / name).write_text("agent changed instructions\n")
(cwd / ".harness2").mkdir()
(cwd / ".harness2/leak").write_text("private state")
(cwd / "output").mkdir(exist_ok=True)
(cwd / "output/report.txt").write_text("report\n")
if Path(sys.argv[0]).name == "codex":
    print(json.dumps({"type": "item.completed", "item": {
          "id": "1", "type": "agent_message", "text": "Finished the change."}}), flush=True)
    print(json.dumps({"type": "turn.completed", "usage": {
          "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}}), flush=True)
else:
    print(json.dumps({"type": "system", "subtype": "init", "model": "fake"}), flush=True)
    print(json.dumps({"type": "assistant", "message": {"content": [
          {"type": "tool_use", "name": "Write", "input": {"file_path": "code.txt"}},
          {"type": "text", "text": "Finished the change."}]}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "result": "Finished the change.",
                      "total_cost_usd": 0.125}), flush=True)
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""


class SuiteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / ".harness2/tasks").mkdir(parents=True)
        self.tasks_dir = self.project / ".harness2/tasks"
        (self.project / "code.txt").write_text("before\n")
        (self.project / "CLAUDE.md").write_text("Read the code first.\n")
        (self.project / "AGENTS.md").write_text("Read the code first.\n")
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        binary = self.root / "bin"
        binary.mkdir()
        for name in ("claude", "codex"):
            path = binary / name
            path.write_text(f"#!{sys.executable}\n" + FAKE_CLI)
            path.chmod(0o755)
        self.record = self.root / "records.jsonl"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HARNESS2_", "FAKE_"))}
        env.update(
            PATH=f"{binary}{os.pathsep}{env['PATH']}",
            FAKE_RECORD=str(self.record),
            XDG_CONFIG_HOME=str(self.root / "config"),
            ANTHROPIC_API_KEY="fake-key",
            OPENAI_API_KEY="fake-key",
        )
        environment = patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(events.start, None)
        events.start(None)
        self.task("one", domain="coding", split="train")
        workspace.commit_all(self.project, "initial")
        self.ctx = self.context()
        self.adapter = suite.make_adapter(self.ctx)

    def context(self, **overrides):
        values = {"executor.model": "anthropic/fake", "improver.model": "anthropic/fake"}
        values.update(overrides)
        settings = load(
            self.project, overrides={key: (value, "test") for key, value in values.items()}
        )
        return registry.SuiteContext(settings, self.project, self.project / ".harness2/runs/test")

    def task(self, task_id, *, files=False, **values):
        task_dir = suite.new_task(
            self.tasks_dir,
            task_id,
            files=files,
            domain=values.pop("domain", "default"),
            check=values.pop("check", None),
        )
        (task_dir / "task.md").write_text("Change code.txt as requested.\n")
        with (task_dir / "task.toml").open("a") as stream:
            for key, value in values.items():
                stream.write(f"{key} = {json.dumps(value)}\n")
        return task_dir

    def records(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def test_module_task_protocol_splits_and_prompt_profiles(self):
        self.assertIs(registry.load("tasks"), suite)
        self.assertIsInstance(self.adapter, BenchmarkAdapter)
        self.assertEqual(self.adapter.tasks(), ["one"])
        self.assertEqual(self.adapter.tasks("train"), ["one"])
        self.assertEqual(self.adapter.tasks("test"), [])
        self.assertEqual(self.adapter.domain_of("one"), "coding")
        self.assertIsNone(self.adapter.task_files("one"))
        self.assertEqual(self.adapter.task_prompt("one"), "Change code.txt as requested.\n")
        self.assertIs(self.adapter.spec.prompts, suite.PROJECT_PROMPTS)
        self.assertEqual(self.adapter.prompts_for("one"), suite.PROJECT_PROMPTS)
        self.adapter.validate_tree({})
        files = self.task("docs", files=True, deliverable="message")
        self.assertEqual(self.adapter.task_files("docs"), files / "files")
        (self.project / "steer.txt").write_text("Use local conventions.")
        ctx = self.context(**{"improver.steer": "@steer.txt", "suite.work": "Maintain the project"})
        prompts = suite.make_adapter(ctx).prompts_for("docs")
        self.assertEqual(prompts.work, "Maintain the project")
        self.assertEqual(prompts.deliverable_word, "answer")
        self.assertEqual(prompts.domain_guidance, "Use local conventions.")
        self.assertEqual(prompts.yardstick, suite.FILES_PROMPTS.yardstick)
        for overrides in ({"suite.work": "rubric"}, {"improver.steer": "score"}):
            with self.assertRaisesRegex(ConfigError, "improver.steer / suite.work:"):
                suite.make_adapter(self.context(**overrides)).prompts_for("one")
        with self.assertRaisesRegex(ConfigError, "cannot read improver.steer"):
            suite.make_adapter(self.context(**{"improver.steer": "@missing"})).prompts_for("one")
        with self.assertRaises(ConfigError):
            self.adapter.tasks("missing")

    def test_a_rollout_that_died_before_writing_anything_reads_as_empty(self):
        dead = self.root / "missing"
        self.assertFalse(self.adapter.delivered(dead))
        self.assertEqual(self.adapter.deliverable_files(dead), {})
        self.assertEqual(self.adapter.deliverable_text(dead), "")
        self.assertEqual(self.adapter.rollout_cost(dead), 0.0)

    def test_claude_execute_capture_idempotence_and_isolation(self):
        self.assertFalse(self.adapter.delivered(self.root / "missing"))
        run_dir = self.adapter.execute("one", None, "nested/run")
        self.assertEqual(run_dir.name, "nested__run")
        self.assertTrue(runmark.is_complete(run_dir, harness_dir=self.adapter.base_harness()))
        self.assertTrue(self.adapter.delivered(run_dir))
        self.assertEqual(self.adapter.rollout_cost(run_dir), 0.125)
        artifacts = self.adapter.deliverable_files(run_dir)
        patch_text = artifacts["deliverable.patch"]
        self.assertIn("+after", patch_text)
        self.assertNotIn("CLAUDE.md", patch_text)
        self.assertNotIn(".harness2", patch_text)
        self.assertEqual(
            self.adapter.deliverable_text(run_dir), patch_text + "\n\nFinished the change."
        )
        self.assertIn("[ACTION] Write:", self.adapter.trajectory(run_dir, obs_trunc=10000))
        meta = json.loads((run_dir / "meta.json").read_text())
        self.assertTrue(meta["ok"])
        self.assertEqual(meta["exit_code"], 0)
        first = self.records()[0]
        self.assertEqual(first["prompt"], self.adapter.task_prompt("one"))
        self.assertEqual(first["claude_home"], str(run_dir / "claude-home"))
        self.assertFalse(Path(first["cwd"]).exists())
        self.assertEqual(self.adapter.execute("one", None, "nested/run"), run_dir)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual((self.project / "code.txt").read_text(), "before\n")
        (run_dir / "deliverable.patch").write_text("")
        self.assertFalse(self.adapter.delivered(run_dir))

    def test_changed_candidate_reexecutes_and_drops_stale_judge(self):
        candidate = self.root / "candidate"
        surface = self.adapter.substrate.surface()
        tree.write_tree(surface, {"CLAUDE.md": "First candidate\n"}, candidate)
        run_dir = self.adapter.execute("one", candidate, "candidate")
        (run_dir / "judge.json").write_text('{"n_passed": 0}')
        (candidate / "CLAUDE.md").write_text("Second candidate\n")
        self.adapter.execute("one", candidate, "candidate")
        self.assertEqual(len(self.records()), 2)
        self.assertFalse((run_dir / "judge.json").exists())

    def test_codex_stream_and_provider_home(self):
        ctx = self.context(**{"executor.name": "codex", "executor.model": "openai/fake"})
        adapter = suite.make_adapter(ctx)
        run_dir = adapter.execute("one", None, "codex")
        self.assertTrue(adapter.delivered(run_dir))
        self.assertEqual(adapter.rollout_cost(run_dir), 0)
        self.assertNotIn("AGENTS.md", adapter.deliverable_text(run_dir))
        self.assertIn("Finished the change.", adapter.deliverable_text(run_dir))
        self.assertIn("tokens in=10", adapter.trajectory(run_dir, obs_trunc=10000))
        record = self.records()[0]
        self.assertEqual(record["codex_home"], str(run_dir / "codex-home"))
        self.assertIn('model = "fake"', record["config"])
        self.assertEqual(record["argv"][-1], "-")
        self.assertFalse(adapter.spec.allow_subagents)

    def test_agent_failure_and_timeout_are_completed(self):
        for run_id, env, kwargs, expected in (
            ("failed", {"FAKE_RC": "7"}, {}, 7),
            ("timeout", {"FAKE_SLEEP": "2"}, {"timeout": 0.1}, -1),
        ):
            with self.subTest(run_id=run_id), patch.dict(os.environ, env):
                run_dir = self.adapter.execute("one", None, run_id, **kwargs)
                meta = json.loads((run_dir / "meta.json").read_text())
                self.assertFalse(meta["ok"])
                self.assertEqual(meta["exit_code"], expected)
                self.assertEqual(meta["timed_out"], run_id == "timeout")
                self.assertTrue(
                    runmark.is_complete(run_dir, harness_dir=self.adapter.base_harness())
                )
                count = len(self.records())
                self.adapter.execute("one", None, run_id, **kwargs)
                self.assertEqual(len(self.records()), count)

    def test_task_timeout_takes_precedence(self):
        self.task("bounded", timeout=1)
        with patch.dict(os.environ, {"FAKE_SLEEP": "0.02"}):
            run_dir = self.adapter.execute("bounded", None, "bounded", timeout=0.001)
        self.assertTrue(json.loads((run_dir / "meta.json").read_text())["ok"])

    def test_files_command_executor_and_binary_deliverables(self):
        task_dir = self.task("files", files=True, check="test -s output/report.txt")
        (task_dir / "files/source.txt").write_text("supplied source\n")
        script = self.root / "command.py"
        script.write_text(
            "import pathlib, sys\n"
            "ws, prompt, output = map(pathlib.Path, sys.argv[1:])\n"
            "assert ws == pathlib.Path.cwd()\n"
            "assert 'Save your deliverables in the output/ directory.' in prompt.read_text()\n"
            "(output / 'report.txt').write_text((ws / 'source.txt').read_text())\n"
            "(output / 'binary.dat').write_bytes(b'\\xff\\x00')\n"
            "print('Saved report.')\n"
        )
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} {{workspace}} {{prompt_file}} {{output}}"
        adapter = suite.make_adapter(
            self.context(
                **{
                    "executor.name": "command",
                    "executor.command": command,
                    "executor.editable": ["instructions.txt"],
                }
            )
        )
        (self.project / "instructions.txt").write_text("Use the source.\n")
        run_dir = adapter.execute("files", None, "files")
        self.assertTrue(adapter.delivered(run_dir))
        self.assertEqual(adapter.rollout_cost(run_dir), 0)
        self.assertEqual(
            adapter.deliverable_files(run_dir),
            {
                "binary.dat": "(binary file, 2 bytes)",
                "report.txt": "supplied source\n",
            },
        )
        self.assertIn("supplied source", adapter.deliverable_text(run_dir))
        self.assertIn("Saved report.", adapter.trajectory(run_dir, obs_trunc=10000))
        self.assertTrue(adapter.score(run_dir, "files")["all_pass"])
        self.assertTrue(adapter.has_judge("files"))
        shutil.rmtree(run_dir / "output")
        self.assertFalse(adapter.delivered(run_dir))

    def test_message_deliverable(self):
        self.task("message", deliverable="message")
        run_dir = self.adapter.execute("message", None, "message")
        self.assertTrue(self.adapter.delivered(run_dir))
        self.assertEqual(self.adapter.deliverable_text(run_dir), "Finished the change.")
        self.assertEqual(
            self.adapter.deliverable_files(run_dir), {"final.md": "Finished the change."}
        )
        self.assertFalse(self.adapter.has_judge("message"))
        self.assertIsNone(self.adapter.score(run_dir, "message"))
        self.assertFalse((run_dir / "judge.json").exists())

    def test_check_judge_fresh_patch_json_exit_code_cache_and_failure(self):
        task_dir = self.tasks_dir / "one"
        (task_dir / "check").mkdir()
        checker = task_dir / "check/probe.py"
        checker.write_text(
            "import os, pathlib\n"
            "assert pathlib.Path('code.txt').read_text() == 'after\\n'\n"
            "assert pathlib.Path('CLAUDE.md').read_text() == 'Read the code first.\\n'\n"
            "assert pathlib.Path(os.environ['HARNESS2_RUN_DIR']).is_dir()\n"
            "assert pathlib.Path(os.environ['HARNESS2_TASK_DIR']).is_dir()\n"
            "print('detail')\n"
            'print(\'{"n_passed": 2, "n_criteria": 3}\')\n'
        )
        with (task_dir / "task.toml").open("a") as stream:
            stream.write(f"check = {json.dumps(shlex.quote(sys.executable) + ' check/probe.py')}\n")
        self.assertTrue(self.adapter.has_judge("one"))
        run_dir = self.adapter.execute("one", None, "judge")
        result = self.adapter.score(run_dir, "one")
        self.assertEqual((result["n_passed"], result["n_criteria"], result["exit_code"]), (2, 3, 0))
        self.assertFalse(result["all_pass"])
        with patch("harness2.suite.workspace.project_workspace") as create_workspace:
            self.assertEqual(self.adapter.score(run_dir, "one"), result)
            create_workspace.assert_not_called()
        checker.write_text("raise RuntimeError('changed checker')")
        changed = self.adapter.score(run_dir, "one")
        self.assertEqual(changed["n_passed"], 0)
        self.assertNotEqual(changed["identity"]["check_sha256"], result["identity"]["check_sha256"])
        for command, expected in (
            ("exit 0", 1),
            ("exit 3", 0),
            ('echo \'{"n_passed": true, "n_criteria": 1}\'; exit 4', 0),
        ):
            (task_dir / "task.toml").write_text(f"check = {json.dumps(command)}\n")
            result = self.adapter.score(run_dir, "one")
            self.assertEqual((result["n_passed"], result["n_criteria"]), (expected, 1))
            self.assertEqual(result["identity"]["check"], command)
        (run_dir / "judge.json").unlink()
        (run_dir / "deliverable.patch").write_text("not a patch")
        self.assertIsNone(self.adapter.score(run_dir, "one"))
        self.assertFalse((run_dir / "judge.json").exists())

    def test_check_timeout_is_a_failed_judgment(self):
        task_dir = self.task("slow", timeout=1)
        command = f"exec {shlex.quote(sys.executable)} -c " + shlex.quote(
            "import time; print('started', flush=True); time.sleep(2)"
        )
        with (task_dir / "task.toml").open("a") as stream:
            stream.write(f"check = {json.dumps(command)}\n")
        run_dir = self.adapter.execute("slow", None, "slow")
        result = self.adapter.score(run_dir, "slow")
        self.assertEqual(
            (result["n_passed"], result["n_criteria"], result["exit_code"]), (0, 1, -1)
        )
        self.assertIn("started", result["output_tail"])

    def test_check_cache_hashes_contents_names_and_empty_directories(self):
        task_dir = self.task("check-identity", check="test -f check/ready")
        check_dir = task_dir / "check"
        check_dir.mkdir()
        ready = check_dir / "ready"
        ready.write_bytes(b"first\0content")
        run_dir = self.adapter.execute("check-identity", None, "check-identity")
        first = self.adapter.score(run_dir, "check-identity")
        self.assertEqual(json.loads((run_dir / "judge.json").read_text()), first)
        ready.write_bytes(b"second\0content")
        second = self.adapter.score(run_dir, "check-identity")
        self.assertNotEqual(first["identity"], second["identity"])
        ready.rename(check_dir / "renamed")
        renamed = self.adapter.score(run_dir, "check-identity")
        self.assertFalse(renamed["all_pass"])
        self.assertNotEqual(second["identity"], renamed["identity"])
        (check_dir / "empty").mkdir()
        empty = self.adapter.score(run_dir, "check-identity")
        self.assertNotEqual(renamed["identity"], empty["identity"])
        (check_dir / "compiled.pyc").write_bytes(b"compiled check")
        compiled = self.adapter.score(run_dir, "check-identity")
        self.assertNotEqual(empty["identity"], compiled["identity"])
        for legacy in ('{"n_passed": 1, "n_criteria": 1}', "invalid json", "[]"):
            (run_dir / "judge.json").write_text(legacy)
            self.assertEqual(self.adapter.score(run_dir, "check-identity"), compiled)

    def test_rubric_cache_regrades_after_model_channel_or_rubric_changes(self):
        task_dir = self.tasks_dir / "one"
        rubric = task_dir / "rubric.md"
        rubric.write_text("Three criteria.\n")
        run_dir = self.adapter.execute("one", None, "rubric-identity")
        first = self.adapter.score(run_dir, "one")
        self.assertEqual(first["identity"]["channel"], "claude")
        self.assertEqual(first["identity"]["model"], "anthropic/fake")
        calls = len(self.records())
        output = '{"n_passed": 3, "n_criteria": 3}'
        with patch.dict(os.environ, {"FAKE_JUDGE": output}):
            self.assertEqual(self.adapter.score(run_dir, "one"), first)
            self.assertEqual(len(self.records()), calls)
            changed_model = suite.make_adapter(self.context(**{"judge.model": "anthropic/other"}))
            second = changed_model.score(run_dir, "one")
            self.assertEqual(second["identity"]["model"], "anthropic/other")
            self.assertTrue(second["all_pass"])
            self.assertEqual(len(self.records()), calls + 1)
            self.assertEqual(
                second["identity"]["rubric_sha256"], first["identity"]["rubric_sha256"]
            )
        with patch.dict(os.environ, {"FAKE_JUDGE": '{"n_passed": 0, "n_criteria": 3}'}):
            rubric.write_text("Three changed criteria.\n")
            third = changed_model.score(run_dir, "one")
            self.assertEqual(third["n_passed"], 0)
            self.assertNotEqual(
                third["identity"]["rubric_sha256"], second["identity"]["rubric_sha256"]
            )
            changed_channel = suite.make_adapter(
                self.context(
                    **{
                        "judge.channel": "codex",
                        "judge.model": "openai/fake",
                    }
                )
            )
            fourth = changed_channel.score(run_dir, "one")
            self.assertEqual(fourth["identity"]["channel"], "codex")
            self.assertEqual(fourth["identity"]["model"], "openai/fake")
            self.assertEqual(fourth["n_passed"], 0)
            self.assertEqual(len(self.records()), calls + 3)

    def test_rubric_judge_json_fence_bare_and_unparseable(self):
        task_dir = self.tasks_dir / "one"
        (task_dir / "rubric.md").write_text("Three criteria.\n")
        self.assertTrue(self.adapter.has_judge("one"))
        run_dir = self.adapter.execute("one", None, "rubric")
        for text, expected in (
            ('{"n_passed": 2, "n_criteria": 3, "notes": "one missing"}', 2),
            (
                'Reasoning\n```json\n{"n_passed": 0, "n_criteria": 3}\n```\n'
                '```json\n{"n_passed": 3, "n_criteria": 3}\n```',
                3,
            ),
            ('Reasoning\n{"n_passed": 1, "n_criteria": 3}', 1),
            ('{"n_passed": 4, "n_criteria": 3}', None),
            ("not json", None),
        ):
            with self.subTest(text=text), patch.dict(os.environ, {"FAKE_JUDGE": text}):
                (run_dir / "judge.json").unlink(missing_ok=True)
                result = self.adapter.score(run_dir, "one")
                if expected is None:
                    self.assertIsNone(result)
                    self.assertFalse((run_dir / "judge.json").exists())
                else:
                    self.assertEqual(result["n_passed"], expected)

    def test_base_harness_is_stored_warns_once_and_survives_relocation(self):
        base = self.adapter.base_harness()
        original = tree.read_tree(self.adapter.substrate.surface(), base)
        (self.project / "CLAUDE.md").write_text("changed harness\n")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertEqual(self.adapter.base_harness(), base)
            self.adapter.base_harness()
        self.assertEqual(len(caught), 1)
        self.assertEqual(str(caught[0].message), suite.BASE_DRIFT_WARNING.format(path=base))
        self.assertEqual(tree.read_tree(self.adapter.substrate.surface(), base), original)
        self.adapter.execute("one", None, "stored-base")
        self.assertEqual(self.records()[-1]["harness"]["CLAUDE.md"], original["CLAUDE.md"])
        other = suite.make_adapter(self.ctx)
        with self.assertWarns(UserWarning):
            self.assertEqual(other.base_harness(), base)
        new_root = self.project / ".harness2/runs/moved"
        shutil.move(self.ctx.run_root, new_root)
        moved = suite.make_adapter(replace(self.ctx, run_root=new_root))
        with self.assertWarns(UserWarning):
            self.assertEqual(moved.base_harness(), new_root / "base_harness")
        (self.project / "CLAUDE.md").unlink()
        empty = suite.make_adapter(replace(self.ctx, run_root=self.root / "empty"))
        empty.base_harness()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            empty.base_harness()
        self.assertEqual(caught, [])

    def test_concurrent_base_harness_has_one_complete_snapshot(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(lambda _: self.adapter.base_harness(), range(8)))
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual((paths[0] / "CLAUDE.md").read_text(), "Read the code first.\n")

    def test_task_validation_new_import_jsonl_csv_and_force(self):
        self.assertEqual(suite.list_tasks(self.root / "missing"), [])
        with self.assertRaisesRegex(ConfigError, "already exists"):
            suite.new_task(self.tasks_dir, "one", files=False, domain="x", check=None)
        for task_id in ("../escape", "Upper", "", "/absolute"):
            with self.assertRaises(ConfigError):
                suite.new_task(self.tasks_dir, task_id, files=False, domain="x", check=None)
        source = self.root / "tasks.jsonl"
        source.write_text(
            "\n".join(
                json.dumps(row)
                for row in (
                    {
                        "id": "two",
                        "prompt": "Second request",
                        "domain": "docs",
                        "split": "test",
                        "workspace": "files",
                        "deliverable": "files",
                        "timeout": 10,
                    },
                    {"id": "three", "prompt": "Third request", "check": "exit 0"},
                )
            )
        )
        self.assertEqual(suite.import_tasks(self.tasks_dir, source, force=False), ["two", "three"])
        self.assertEqual((self.tasks_dir / "two/task.md").read_text(), "Second request")
        self.assertEqual(self.adapter.tasks("test"), ["two"])
        self.assertEqual(self.adapter.task_files("two"), self.tasks_dir / "two/files")
        with self.assertRaisesRegex(ConfigError, "already exists"):
            suite.import_tasks(self.tasks_dir, source, force=False)
        self.assertEqual(suite.import_tasks(self.tasks_dir, source, force=True), ["two", "three"])
        source = self.root / "tasks.csv"
        with source.open("w", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=["id", "prompt", "timeout", "split", "check"]
            )
            writer.writeheader()
            writer.writerow({"id": "four", "prompt": "First line\nSecond line", "timeout": 12})
        self.assertEqual(suite.import_tasks(self.tasks_dir, source, force=False), ["four"])
        self.assertEqual((self.tasks_dir / "four/task.md").read_text(), "First line\nSecond line")
        self.assertEqual(suite._read_task(self.tasks_dir, "four")["timeout"], 12)
        for values in (
            {"unknown": 1},
            {"split": "all"},
            {"workspace": "bad"},
            {"deliverable": "bad"},
            {"timeout": 0},
            {"timeout": True},
            {"domain": 1},
        ):
            with self.subTest(values=values):
                (self.tasks_dir / "four/task.toml").write_text(
                    "".join(f"{k} = {json.dumps(v)}\n" for k, v in values.items())
                )
                with self.assertRaises(ConfigError):
                    suite.list_tasks(self.tasks_dir)
        (self.tasks_dir / "four/task.toml").write_text("bad toml")
        with self.assertRaises(ConfigError):
            suite.list_tasks(self.tasks_dir)
        source = self.root / "bad.jsonl"
        for content in (
            '{"id": "bad"}',
            '{"id": "../escape", "prompt": "x"}',
            "[1]",
            "not json",
            '{"id":"dup", "prompt":"x"}\n{"id":"dup", "prompt":"y"}',
        ):
            source.write_text(content)
            with self.assertRaises(ConfigError):
                suite.import_tasks(self.tasks_dir, source, force=False)
        self.assertFalse((self.tasks_dir / "dup").exists())

    def test_force_import_rejects_symlinks_and_malformed_csv(self):
        outside = self.root / "outside.md"
        outside.write_text("keep this")
        (self.tasks_dir / "one/task.md").unlink()
        (self.tasks_dir / "one/task.md").symlink_to(outside)
        source = self.root / "force.jsonl"
        source.write_text('{"id": "one", "prompt": "replace"}')
        with self.assertRaisesRegex(ConfigError, "symlink"):
            suite.import_tasks(self.tasks_dir, source, force=True)
        self.assertEqual(outside.read_text(), "keep this")
        source = self.root / "missing-column.csv"
        source.write_text("id,prompt,timeout\nnew,request\n")
        with self.assertRaisesRegex(ConfigError, "timeout must be a positive integer"):
            suite.import_tasks(self.tasks_dir, source, force=False)

    def test_preflight_reports_all_invalid_tasks_and_config(self):
        self.assertEqual(suite.check(self.ctx), [])
        self.task("template")
        (self.tasks_dir / "template/task.md").write_text(suite.TASK_TEMPLATE)
        self.task("invalid")
        (self.tasks_dir / "invalid/task.toml").write_text('unknown = "key"\n')
        problems = suite.check(
            self.context(**{"executor.bin": "nonexistent-executor", "judge.model": "bad"})
        )
        self.assertTrue(any("binary not found" in p for p in problems))
        self.assertTrue(any("judge: model" in p for p in problems))
        self.assertTrue(
            any("task template: task.md still holds the template text" == p for p in problems)
        )
        self.assertTrue(any("unknown task.toml key 'unknown'" in p for p in problems))
        human = self.context(**{"selector.channel": "human"})
        self.assertEqual(suite.make_adapter(human).spec.channels["selector"].kind, "human")
        with self.assertRaisesRegex(ConfigError, "only allowed for selector"):
            suite.make_adapter(self.context(**{"improver.channel": "human"}))
        with self.assertRaisesRegex(ConfigError, "tasks suite runs"):
            suite.make_adapter(self.context(**{"executor.name": "opencode"}))
        with self.assertRaisesRegex(ConfigError, "non-empty editable"):
            suite.make_adapter(self.context(**{"executor.name": "command"}))
        non_git = self.root / "not-git"
        non_git.mkdir()
        problems = suite.check(replace(self.ctx, project=non_git))
        self.assertTrue(any("needs a git repository" in p for p in problems))

    def test_full_two_task_schedule_k1_k2_and_resume(self):
        self.task("two", domain="coding")
        for k in (1, 2):
            with self.subTest(k=k):
                run_root = self.project / f".harness2/runs/k{k}"
                adapter = suite.make_adapter(replace(self.ctx, run_root=run_root))
                proposer = Proposer(adapter.spec.channels["proposer"], audit_dir=run_root / "audit")
                events.start(run_root / "events.jsonl")
                kwargs = dict(
                    adapter=adapter,
                    prop=proposer,
                    runs_root=run_root,
                    library_root=run_root / "library",
                    modes=("agg", "seq"),
                )
                result = schedule.run_domain("coding", adapter.tasks(), k, **kwargs)
                self.assertEqual((result["done"], result["failed"]), (2, 0), result)
                summaries = list(run_root.glob("tasks-*/**/summary.json"))
                self.assertEqual(len(summaries), 2)
                for path in summaries:
                    summary = json.loads(path.read_text())
                    for mode in ("agg", "seq"):
                        ledger = summary["modes"][mode]["ledger"]
                        self.assertEqual((ledger["R"], ledger["P"]), (4 * k, 3 * k))
                count = len(self.records())
                resumed = schedule.run_domain("coding", adapter.tasks(), k, **kwargs)
                self.assertEqual(
                    [row["status"] for row in resumed["tasks"]], ["skipped", "skipped"]
                )
                self.assertEqual(len(self.records()), count)
                records = [
                    json.loads(line)
                    for line in (run_root / "events.jsonl").read_text().splitlines()
                ]
                rollouts = [row for row in records if row["kind"] == "rollout"]
                self.assertTrue(rollouts)
                self.assertTrue(all(row["cost_usd"] == 0.125 for row in rollouts))
                self.assertTrue(any(row["kind"] == "session" for row in records))
