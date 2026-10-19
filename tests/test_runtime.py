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

"""Offline runtime contracts: channels, accounting, stop/resume, and relocation."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from harness2.core import configure, events, judge, recursion, runmark, schedule
from harness2.core.proposer import SYSTEM_ARG_MAX, Proposer, ProposerError, _call_fingerprint
from harness2.core.spec import CHANNELS, ChannelSpec
from harness2.demo import DemoAdapter, DemoProposer
from harness2.providers import STRIP_ENV, Provider
from harness2.substrate.codex import parse_items, parse_items_text


class EventsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "nested/events.jsonl"
        self.addCleanup(events.start, None)
        events.start(None)

    def test_disabled_until_started_and_request_stop(self):
        with patch.object(events, "_active", False):
            events.emit("session", cost_usd=2)
            events.check()
            self.assertEqual(events.totals()["calls"], 0)
            events.request_stop("requested")
            with self.assertRaisesRegex(events.Stopped, "requested"):
                events.check()
        events.start(None)
        events.check()
        self.assertIsNone(events.totals()["stopped"])

    def test_budget_resume_and_cached_sessions(self):
        events.start(self.path, budget_usd=0.5, max_calls=9)
        events.emit("task_start", task="one")
        events.emit("session", cost_usd=0.2, cached=False)
        events.emit("session", cost_usd=0, cached=True)
        events.emit("rollout", cost_usd=0.3)
        self.assertEqual(
            events.totals(),
            {
                "spent_usd": 0.5,
                "calls": 2,
                "budget_usd": 0.5,
                "max_calls": 9,
                "stopped": "budget_usd",
            },
        )
        with self.assertRaisesRegex(events.Stopped, "budget_usd"):
            events.check()
        events.start(self.path, max_calls=3)
        events.check()
        events.emit("session")
        with self.assertRaisesRegex(events.Stopped, "max_calls"):
            events.check()
        records = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(len(records), 5)
        self.assertTrue(all(record["t"].endswith("Z") for record in records))
        for kwargs in ({"budget_usd": -1}, {"max_calls": -1}):
            with self.assertRaises(ValueError):
                events.start(None, **kwargs)

    def test_concurrent_events_are_whole_lines_and_counted_once(self):
        events.start(self.path)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: events.emit("rollout", task=str(i), cost_usd=0.1), range(40)))
        self.assertEqual(len([json.loads(line) for line in self.path.read_text().splitlines()]), 40)
        self.assertEqual((events.totals()["calls"], events.totals()["spent_usd"]), (40, 4))


FAKE_CLI = r"""import json
import os
import sys
from pathlib import Path

cwd = Path.cwd()
kind = Path(sys.argv[0]).name
record = {"argv": sys.argv[1:], "env": dict(os.environ),
          "stdin": sys.stdin.read() if kind != "opencode" else "",
          "cwd": str(cwd)}
for key, name in (("CODEX_HOME", "config.toml"), ("CLAUDE_CONFIG_DIR", "")):
    if key in os.environ:
        home = Path(os.environ[key])
        record[key + "_exists"] = home.is_dir()
        if name:
            record["config"] = (home / name).read_text()
if (cwd / "opencode.json").is_file():
    record["config"] = json.loads((cwd / "opencode.json").read_text())
    record["mission"] = (cwd / "MISSION.md").read_text()
with open(os.environ["FAKE_RECORD"], "a") as output:
    output.write(json.dumps(record) + "\n")
if "FAKE_OUTPUT" in os.environ:
    print(os.environ["FAKE_OUTPUT"])
elif kind == "claude":
    print(json.dumps({"result": "done", "total_cost_usd": 0.25, "subtype": "success"}))
elif kind == "codex":
    print(json.dumps({"type": "item.completed", "item": {
        "id": "first", "type": "agent_message", "text": "earlier"}}))
    print(json.dumps({"type": "item.completed", "item": {
        "id": "last", "type": "agent_message", "text": "done"}}))
else:
    print(json.dumps({"type": "text", "part": {"text": "done"}}))
    print(json.dumps({"type": "step_finish", "part": {"cost": 0.125}}))
sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""


class ProposerTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        binary = self.root / "bin"
        binary.mkdir()
        for kind in ("claude", "codex", "opencode"):
            path = binary / kind
            path.write_text(f"#!{sys.executable}\n" + FAKE_CLI)
            path.chmod(0o755)
        self.record = self.root / "record.jsonl"
        self.base_env = {
            "PATH": f"{binary}:{os.defpath}",
            "FAKE_RECORD": str(self.record),
            "CUSTOM_KEY": "fake-key",
            "LC_ALL": "C",
        }
        self.environment = patch.dict(
            os.environ, self.base_env | dict.fromkeys(STRIP_ENV, "stale"), clear=True
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(events.start, None)
        events.start(None)

    def records(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def test_channels_alias_empty_and_human(self):
        self.assertEqual(CHANNELS, ("claude", "codex", "opencode", "human"))
        self.assertEqual(ChannelSpec("claude_cli", "local/model").kind, "claude")
        with self.assertRaises(ValueError):
            ChannelSpec("missing", "model")
        with self.assertRaises(ValueError):
            Proposer(ChannelSpec("claude", ""))
        human = Proposer(ChannelSpec("human", ""))
        with self.assertRaisesRegex(ProposerError, "interactive selector"):
            human.call("choose", None, "one")
        self.assertFalse(self.record.exists())

    def test_channel_argv_env_and_provider_config(self):
        providers = {
            "claude": Provider("local", "anthropic", "https://claude.invalid", "CUSTOM_KEY"),
            "codex": Provider("local", "openai", "https://responses.invalid/v1", "CUSTOM_KEY"),
            "opencode": Provider("local", "openai", "https://chat.invalid/v1", "CUSTOM_KEY"),
        }
        for kind, provider in providers.items():
            with self.subTest(kind=kind):
                prop = Proposer(ChannelSpec(kind, "local/test-model", provider=provider))
                reply = prop.call("mission", None, kind, system="system")
                self.assertEqual(reply.text, "done")
                self.assertEqual(
                    reply.cost_usd, {"claude": 0.25, "codex": 0, "opencode": 0.125}[kind]
                )
                record = self.records()[-1]
                cwd = record["cwd"]
                expected_env = dict(self.base_env)
                if kind == "claude":
                    self.assertEqual(
                        record["argv"],
                        [
                            "--dangerously-skip-permissions",
                            "-p",
                            "--output-format",
                            "json",
                            "--effort",
                            "high",
                            "--model",
                            "test-model",
                            "--setting-sources",
                            "project,local",
                            "--append-system-prompt",
                            "system",
                        ],
                    )
                    expected_env |= {
                        "ANTHROPIC_API_KEY": "fake-key",
                        "ANTHROPIC_BASE_URL": provider.base_url,
                        "CLAUDE_CONFIG_DIR": cwd + "/.claude-home",
                    }
                    self.assertTrue(record["CLAUDE_CONFIG_DIR_exists"])
                    self.assertEqual(record["stdin"], "mission")
                elif kind == "codex":
                    self.assertEqual(
                        record["argv"],
                        [
                            "exec",
                            "--json",
                            "--skip-git-repo-check",
                            "--dangerously-bypass-approvals-and-sandbox",
                            "--ephemeral",
                            "-C",
                            cwd,
                            "-",
                        ],
                    )
                    expected_env["CODEX_HOME"] = cwd + "/.codex-home"
                    self.assertTrue(record["CODEX_HOME_exists"])
                    self.assertEqual(
                        record["stdin"], "# SYSTEM INSTRUCTIONS\n\nsystem\n\n# END\n\nmission"
                    )
                    config = tomllib.loads(record["config"])
                    self.assertEqual(config["model"], "test-model")
                    self.assertEqual(config["model_provider"], "harness2-local")
                    self.assertEqual(
                        config["model_providers"]["harness2-local"]["env_key"], "CUSTOM_KEY"
                    )
                else:
                    self.assertEqual(
                        record["argv"],
                        [
                            "run",
                            "Read MISSION.md first. Do what it asks. "
                            "Reply with the requested output as your final message AND save it verbatim to output/answer.md.",
                            "--dir",
                            cwd,
                            "--model",
                            "local/test-model",
                            "--format",
                            "json",
                            "--variant",
                            "high",
                        ],
                    )
                    expected_env |= {
                        "OPENCODE_PERMISSION": json.dumps(
                            {
                                "external_directory": {
                                    "*": "deny",
                                    cwd: "allow",
                                    cwd + "/**": "allow",
                                }
                            }
                        ),
                        "XDG_DATA_HOME": cwd + "/.ocdata",
                        "XDG_STATE_HOME": cwd + "/.ocstate",
                    }
                    self.assertEqual(record["stdin"], "")
                    self.assertEqual(record["mission"], "system\n\n---\n\nmission")
                    self.assertEqual(record["config"]["agent"]["build"]["steps"], 120)
                    self.assertEqual(
                        record["config"]["provider"]["local"]["models"], {"test-model": {}}
                    )
                observed_env = dict(record["env"])
                # macOS injects this into the child after subprocess receives its environment.
                observed_env.pop("__CF_USER_TEXT_ENCODING", None)
                self.assertEqual(observed_env, expected_env)
                normalized = json.dumps(
                    {"argv": [kind, *record["argv"]], "env": record["env"]}, sort_keys=True
                )
                normalized = normalized.replace(cwd, "<tmp>").replace(str(self.root), "<fixture>")
                print(f"PROPOSER {kind}: {normalized}")

    def test_no_provider_custom_binary_long_system_and_codex_config(self):
        system = "s" * (SYSTEM_ARG_MAX + 1)
        binary = f'"{self.root}/bin/claude" --wrapper'
        Proposer(ChannelSpec("claude", "bare-model", bin=binary)).call(
            "mission", None, "long", system=system
        )
        record = self.records()[-1]
        self.assertEqual(record["argv"][0], "--wrapper")
        self.assertEqual(record["stdin"], f"# SYSTEM INSTRUCTIONS\n\n{system}\n\n# END\n\nmission")
        self.assertFalse(any(key in record["env"] for key in STRIP_ENV))
        Proposer(ChannelSpec("codex", "bare-model")).call("mission", None, "plain")
        self.assertEqual(
            tomllib.loads(self.records()[-1]["config"]),
            {"model": "bare-model", "model_reasoning_effort": "high"},
        )

    def test_vertex_env_is_explicit(self):
        provider = Provider("vertex", "vertex", project="test-project", region="test-region")
        prop = Proposer(ChannelSpec("opencode", "google-vertex/test-model", provider=provider))
        prop.call("mission", None, "vertex")
        record = self.records()[-1]
        self.assertEqual(record["env"]["GOOGLE_VERTEX_PROJECT"], "test-project")
        self.assertEqual(record["env"]["GOOGLE_VERTEX_LOCATION"], "test-region")
        self.assertEqual(record["env"]["VERTEXAI_LOCATION"], "test-region")
        self.assertNotIn("provider", record["config"])

    def test_codex_failure_events_and_returncode_emit_failed_sessions(self):
        for output, rc in (
            (json.dumps({"type": "turn.failed", "error": {"message": "failed"}}), "0"),
            (json.dumps({"type": "error", "message": "bad"}), "0"),
            (
                json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}}
                ),
                "2",
            ),
        ):
            with (
                self.subTest(output=output, rc=rc),
                patch.dict(os.environ, {"FAKE_OUTPUT": output, "FAKE_RC": rc}),
            ):
                events.start(None)
                prop = Proposer(ChannelSpec("codex", "local/model"), attempts=1)
                with self.assertRaises(ProposerError):
                    prop.call("mission", None, "failure")
                self.assertEqual(events.totals()["calls"], 1)
                self.assertFalse(prop._calls[-1].ok)

    def test_cache_budget_harvest_and_fingerprint_relocation(self):
        context = self.root / "run/context/task"
        (context / "harness").mkdir(parents=True)
        (context / "harness/CLAUDE.md").write_text("base")
        audit = self.root / "run/audit"
        prop = Proposer(ChannelSpec("claude", "local/model"), audit_dir=audit)
        events.start(None, max_calls=1)
        reply = prop.call("mission", context, "one", harvest="context/harness")
        self.assertEqual((reply.workspace / "CLAUDE.md").read_text(), "base")
        fingerprint = _call_fingerprint("one", "mission", "", context, audit)
        moved = self.root / "moved"
        shutil.move(self.root / "run", moved)
        context, audit = moved / "context/task", moved / "audit"
        self.assertEqual(_call_fingerprint("one", "mission", "", context, audit), fingerprint)
        prop = Proposer(prop.spec, audit_dir=audit)
        self.assertTrue(prop.call("mission", context, "one", harvest="context/harness").cached)
        self.assertEqual((len(self.records()), events.totals()["calls"]), (1, 1))
        with self.assertRaises(events.Stopped):
            prop.call("different", context, "two")
        shutil.rmtree(prop._calls[-1].workspace)
        with self.assertRaises(events.Stopped):
            prop.call("mission", context, "one", harvest="context/harness")

    def test_jsonl_text_and_path_share_deduplication(self):
        text = (
            "junk\n"
            + "\n".join(
                json.dumps(record)
                for record in [
                    {
                        "type": "item.started",
                        "item": {"id": "x", "type": "agent_message", "text": "old"},
                    },
                    {"type": "turn.completed"},
                    {
                        "type": "item.completed",
                        "item": {"id": "x", "type": "agent_message", "text": "last"},
                    },
                ]
            )
            + "\n{invalid\n"
        )
        path = self.root / "stream.jsonl"
        path.write_text(text)
        self.assertEqual(parse_items_text(text), parse_items(path))
        self.assertEqual(
            [record["type"] for record in parse_items_text(text)],
            ["item.completed", "turn.completed"],
        )
        self.assertEqual(parse_items_text(text)[0]["item"]["text"], "last")
        self.assertEqual(parse_items(self.root / "missing")[0]["type"], "_read_error")


class RuntimeWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "original"
        self.adapter = DemoAdapter(self.root)
        self.prop = DemoProposer(
            self.adapter.spec.channels["proposer"], audit_dir=self.adapter.spec.runs_root / "audit"
        )
        self.addCleanup(events.start, None)
        self.addCleanup(configure, phase_width=2, transport_attempts=2, proposer_concurrency=2)
        events.start(None)

    def sweep(self, k=1, **kwargs):
        return schedule.run_sweep(
            k,
            adapter=self.adapter,
            prop=self.prop,
            runs_root=self.adapter.spec.runs_root,
            library_root=self.root / "library",
            **kwargs,
        )

    def test_budget_stop_resume_without_repeated_work(self):
        event_path = self.adapter.spec.runs_root / "events.jsonl"
        events.start(event_path, max_calls=3)
        with self.assertRaisesRegex(events.Stopped, "max_calls"):
            self.sweep()
        self.assertEqual(events.totals()["calls"], 3)
        before = self.adapter.execution_count
        self.assertEqual(before, 1)
        events.start(event_path, max_calls=100)
        result = self.sweep()
        self.assertEqual(result["domains"][0]["done"], 2)
        records = [json.loads(line) for line in event_path.read_text().splitlines()]
        rollouts = [record["run_id"] for record in records if record["kind"] == "rollout"]
        self.assertEqual(len(rollouts), len(set(rollouts)))
        self.assertEqual(self.adapter.execution_count, 14)
        self.assertEqual(events.totals()["calls"], 30)
        executions, calls = self.adapter.execution_count, len(self.prop._calls)
        self.sweep()
        self.assertEqual((self.adapter.execution_count, len(self.prop._calls)), (executions, calls))
        print(
            f"BUDGET STOP/RESUME: stopped=3 calls; executions={executions}; "
            f"total_calls={events.totals()['calls']}; repeated_rollouts=0"
        )

    def test_relocation_skips_executions_and_proposals_and_resolves_judge_paths(self):
        self.sweep(k=2)
        moved = self.root.with_name("moved")
        shutil.move(self.root, moved)
        self.root = moved
        self.adapter = DemoAdapter(moved)
        self.prop = DemoProposer(self.prop.spec, audit_dir=self.adapter.spec.runs_root / "audit")
        self.adapter.score = Mock(
            side_effect=lambda rd, task: (
                {"n_passed": 1, "n_criteria": 1} if self.adapter.delivered(rd) else None
            )
        )
        result = self.sweep(k=2)
        self.assertEqual(
            [row["status"] for row in result["domains"][0]["tasks"]], ["skipped", "skipped"]
        )
        self.assertEqual((self.adapter.execution_count, len(self.prop._calls)), (0, 0))
        selections = []
        for summary in self.adapter.spec.runs_root.glob("demo-oc-agg2/*/summary.json"):
            data = json.loads(summary.read_text())
            for row in data["base"]:
                self.assertFalse(Path(row["run_dir"]).is_absolute())
                self.assertTrue(
                    runmark.resolve_path(row["run_dir"], self.adapter.spec.runs_root).is_dir()
                )
            for selection in data["selections"].values():
                self.assertFalse(Path(selection["chosen_run_dir"]).is_absolute())
                selections.append(selection)
        for record in self.adapter.spec.runs_root.rglob("selection.json"):
            selection = json.loads(record.read_text())
            self.assertFalse(Path(selection["chosen_run_dir"]).is_absolute())
        for record in self.adapter.spec.runs_root.rglob(runmark.MARK):
            data = json.loads(record.read_text())
            if "rollout" in data:
                self.assertFalse(Path(data["rollout"]["run_dir"]).is_absolute())
        rows = judge.judge_selections(self.adapter, selections)
        self.assertTrue(all(row.graded for row in rows))
        absolute = dict(selections[0])
        absolute["chosen_run_dir"] = str(
            runmark.resolve_path(absolute["chosen_run_dir"], self.adapter.spec.runs_root)
        )
        self.assertTrue(judge.judge_selections(self.adapter, [absolute])[0].graded)
        print(f"RELOCATION: executions=0; proposer_calls=0; judged={len(rows)}; relative_paths=ok")

    def test_reflector_prop_and_distinct_selector_are_threaded(self):
        reflector = DemoProposer(self.prop.spec)
        selector = DemoProposer(self.prop.spec)
        self.sweep(k=2, selector_prop=selector, reflector_prop=reflector)
        self.assertTrue(selector._calls)
        self.assertTrue(all(call.label.startswith("select.") for call in selector._calls))
        self.assertEqual(len(reflector._calls), 4)
        self.assertTrue(all(call.label.startswith("experience.") for call in reflector._calls))
        self.assertFalse(any(call.label.startswith("experience.") for call in self.prop._calls))
        spec = ChannelSpec("codex", "local/model")
        self.adapter.spec.channels["selector"] = spec
        resolved = schedule._selector_for(self.adapter, self.prop, None)
        self.assertEqual(resolved.spec, spec)

    def test_configure_reaches_existing_functions_without_default_capture(self):
        configure(phase_width=1, transport_attempts=1, proposer_concurrency=1)
        self.assertEqual(
            (recursion.PHASE_WIDTH, schedule.ROLLOUT_PARALLEL, recursion.TRANSPORT_ATTEMPTS),
            (1, 1, 1),
        )
        import harness2.core.proposer as proposer_module

        self.assertTrue(proposer_module._SEM.acquire(blocking=False))
        self.assertFalse(proposer_module._SEM.acquire(blocking=False))
        proposer_module._SEM.release()
        widths = []
        real_pool = ThreadPoolExecutor

        def pool(*args, **kwargs):
            widths.append(kwargs["max_workers"])
            return real_pool(*args, **kwargs)

        with patch.object(recursion, "ThreadPoolExecutor", side_effect=pool):
            self.sweep()
        self.assertTrue(widths)
        self.assertEqual(set(widths), {1})
        for kwargs in (
            {"phase_width": 0},
            {"transport_attempts": -1},
            {"proposer_concurrency": False},
        ):
            with self.assertRaises(ValueError):
                configure(**kwargs)

    def test_rollout_cost_failures_cache_and_stage_stop(self):
        self.adapter.rollout_cost = Mock(return_value=0.5)
        ledger = recursion.Ledger("agg", "ledger-1", 1)
        work = self.adapter.spec.runs_root / "work"
        rv = recursion._roll(
            self.adapter, ledger, "ledger-1", "base1", None, lambda label: label, work
        )
        self.assertEqual((rv.cost_usd, events.totals()["spent_usd"]), (0.5, 0.5))
        recursion._roll(self.adapter, ledger, "ledger-1", "base1", None, lambda label: label, work)
        self.assertEqual((self.adapter.execution_count, events.totals()["calls"]), (1, 1))
        events.request_stop("requested")
        with self.assertRaises(events.Stopped):
            recursion._run_stage(
                label="inv1",
                dim="INV",
                job="edit",
                fallback={},
                adapter=self.adapter,
                prop=self.prop,
                ctx_dir=work,
                task_id="ledger-1",
                rollouts=[],
                harness_dir=None,
                library_dir=None,
            )
        events.start(None)
        with patch.object(self.adapter, "execute", side_effect=RuntimeError("failed")):
            failed = recursion._roll(
                self.adapter, ledger, "ledger-1", "failed", None, lambda label: label, work
            )
        self.assertFalse(failed.ok)
        self.assertIn("failed", failed.error)
        with patch.object(self.adapter, "execute", side_effect=events.Stopped("requested")):
            with self.assertRaises(events.Stopped):
                recursion._roll(
                    self.adapter, ledger, "ledger-1", "stopped", None, lambda label: label, work
                )

    def test_batch_bases_reuse_cache_and_propagate_stop(self):
        self.adapter.execute_many = Mock(
            side_effect=lambda task, harness, ids: [
                self.adapter.execute(task, harness, rid) for rid in ids
            ]
        )
        pending = [("base1", "first"), ("base2", "second")]
        work = self.adapter.spec.runs_root / "batch"
        got = schedule._roll_bases("ledger-1", self.adapter, pending, work / "missing")
        self.assertEqual(set(got), {"base1", "base2"})
        self.assertEqual(events.totals()["calls"], 2)
        schedule._roll_bases("ledger-1", self.adapter, pending, work / "missing")
        self.adapter.execute_many.assert_called_once()
        self.adapter.execute_many.side_effect = events.Stopped("batch stopped")
        with self.assertRaises(events.Stopped):
            schedule._roll_bases(
                "ledger-1",
                self.adapter,
                [("base3", "third"), ("base4", "fourth")],
                work / "missing",
            )

    def test_sweep_collects_other_domains_before_raising_and_interrupt_requests_stop(self):
        completed = threading.Event()

        def domain(name, *args, **kwargs):
            if name == "first":
                raise events.Stopped("limit")
            completed.set()
            return {"domain": name}

        with (
            patch.object(schedule, "task_stream", return_value={"first": ["a"], "second": ["b"]}),
            patch.object(schedule, "run_domain", side_effect=domain),
        ):
            with self.assertRaisesRegex(events.Stopped, "limit"):
                self.sweep(domain_parallel=2)
        self.assertTrue(completed.is_set())
        with (
            patch.object(schedule, "run_domain", return_value={}),
            patch.object(schedule, "as_completed", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.sweep()
        self.assertEqual(events.totals()["stopped"], "interrupted")

    def test_relative_and_legacy_absolute_paths(self):
        inside = self.root / "runs/one"
        outside = self.root / "elsewhere"
        self.assertEqual(runmark.relative_path(inside, self.root / "runs"), "one")
        self.assertEqual(runmark.relative_path(outside, self.root / "runs"), str(outside.resolve()))
        self.assertEqual(runmark.resolve_path("one", self.root / "runs"), inside)
        self.assertEqual(runmark.resolve_path(outside, self.root / "runs"), outside)


if __name__ == "__main__":
    unittest.main()
