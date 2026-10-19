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

"""Subscription isolation and resumable usage limits against offline CLIs."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness2 import providers, settings, suite, workspace
from harness2.cli.main import main
from harness2.core import events, recursion, runmark, schedule
from harness2.core.proposer import Proposer
from harness2.core.spec import ChannelSpec
from harness2.registry import SuiteContext
from harness2.settings import ConfigError

FAKE_CLI = r"""import json, os, sys
from pathlib import Path

argv = sys.argv[1:]
kind = Path(sys.argv[0]).name
if "--version" in argv:
    print("fixture 1.0")
    sys.exit(0)
status = argv in (["login", "status"], ["auth", "status"])
cwd = Path.cwd()
home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
config = home / "config.toml"
record = {"kind": kind, "argv": argv, "env": dict(os.environ), "cwd": str(cwd),
          "stdin": "" if status else sys.stdin.read(),
          "agents_exists": (home / "AGENTS.md").exists(),
          "hooks_exists": (home / "hooks.json").exists(),
          "config_exists": config.exists(),
          "config": config.read_text() if config.exists() else None}
with open(os.environ["FAKE_RECORD"], "a") as log:
    log.write(json.dumps(record) + "\n")
if status:
    sys.exit(int(os.environ.get("FAKE_LOGIN_RC", "0")))
improver = ("--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
            or (cwd / "context").is_dir())
role = "improver" if improver else "executor"
if os.environ.get("LIMIT_ROLE") == role:
    limit = os.environ.get("LIMIT_TEXT", "rate-limit reached")
    source = os.environ.get("LIMIT_SOURCE", "event")
    if source == "event":
        if kind == "claude":
            print(json.dumps({"type": "result", "subtype": "error_during_execution",
                              "is_error": True, "errors": [limit]}))
        else:
            print(json.dumps({"type": "turn.failed", "error": {"message": limit}}))
        sys.exit(0)
    print(limit, file=sys.stderr if source == "stderr" else sys.stdout)
    sys.exit(7)
if improver:
    context = cwd / "context"
    if (context / "candidates").is_dir():
        chosen = sorted((context / "candidates").glob("*.md"))[0].stem
        text = "<why>Complete.</why>\n```choice\n" + chosen + "\n```"
    elif (context / "adoption.json").is_file():
        text = "NO_ENTRY"
    else:
        for name in ("CLAUDE.md", "AGENTS.md"):
            harness = context / "harness" / name
            if harness.is_file():
                harness.write_text(harness.read_text() + "Check the output.\n")
        text = "<notes>Check the output.</notes>"
else:
    (cwd / "output").mkdir(exist_ok=True)
    (cwd / "output/report.txt").write_text("done\n")
    text = "done"
if kind == "claude":
    print(json.dumps({"type": "result", "subtype": "success", "result": text,
                      "total_cost_usd": 0.01}))
else:
    print(json.dumps({"type": "item.completed", "item": {
        "id": "reply", "type": "agent_message", "text": text}}))
"""


class SubscriptionTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        self.tasks = self.project / ".harness2/tasks"
        self.tasks.mkdir(parents=True)
        (self.project / "CLAUDE.md").write_text("Read the task.\n")
        (self.project / "AGENTS.md").write_text("Read the task.\n")
        binary = self.root / "bin"
        binary.mkdir()
        for name in ("claude", "codex"):
            path = binary / name
            path.write_text(f"#!{sys.executable}\n" + FAKE_CLI)
            path.chmod(0o755)
        self.record = self.root / "records.jsonl"
        self.user_home = self.root / "user"
        for name in (".codex", ".claude"):
            path = self.user_home / name
            path.mkdir(parents=True)
            (path / "config.toml").write_text("private user config")
            (path / "auth.json").write_text("private user auth")
        (self.user_home / ".codex/AGENTS.md").write_text("Private user instructions.\n")
        (self.user_home / ".codex/hooks.json").write_text('{"hooks": {}}\n')
        environment = patch.dict(
            os.environ,
            {
                "PATH": f"{binary}{os.pathsep}{os.defpath}",
                "HOME": str(self.user_home),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
                "FAKE_RECORD": str(self.record),
                "CLAUDE_CODE_OAUTH_TOKEN": "fixture-oauth",
                "ANTHROPIC_API_KEY": "ambient-anthropic",
                "OPENAI_API_KEY": "ambient-openai",
                "CODEX_API_KEY": "ambient-codex",
                "ANTHROPIC_BASE_URL": "https://ambient.invalid",
                "OPENAI_BASE_URL": "https://ambient.invalid",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        events.start(None)
        self.addCleanup(events.start, None)
        task = suite.new_task(self.tasks, "one", files=True, domain="coding", check="true")
        (task / "task.md").write_text("Produce a report.")

    def adapter(self, name="test", **overrides):
        values = {
            "executor.model": "claude-login/claude-sonnet-5-5",
            "improver.model": "claude-login/claude-sonnet-5-5",
        }
        values.update(overrides)
        config = settings.load(
            self.project, overrides={key: (value, "test") for key, value in values.items()}
        )
        return suite.make_adapter(
            SuiteContext(config, self.project, self.project / ".harness2/runs" / name)
        )

    def records(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def launch_record(self, label):
        record = self.records()[-1]
        env = dict(record["env"])
        env.pop("__CF_USER_TEXT_ENCODING", None)
        normalized = json.dumps(
            {"argv": [record["kind"], *record["argv"]], "env": env}, sort_keys=True
        )
        normalized = normalized.replace(record["cwd"], "<workspace>")
        normalized = normalized.replace(str(self.root), "<fixture>")
        print(f"SUBSCRIPTION {label}: {normalized}")
        return record

    def test_builtin_providers_use_cli_auth(self):
        config = settings.load(self.project)
        for name in ("claude-login", "chatgpt-login"):
            self.assertEqual(config.provider(name).type, name)
        self.assertEqual(providers.claude_env(config.provider("claude-login")), {})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(providers.claude_env(config.provider("claude-login")), {})

    def test_claude_subscription_executor_and_improver_launches(self):
        adapter = self.adapter()
        result = adapter.execute("one", None, "claude")
        record = self.launch_record("claude-login executor")
        self.assertEqual(
            record["argv"],
            [
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--dangerously-skip-permissions",
                "--setting-sources",
                "project,local",
                "--model",
                "claude-sonnet-5-5",
                "--effort",
                "high",
                "--strict-mcp-config",
                "--no-session-persistence",
            ],
        )
        self.assertFalse((result / "claude-home").exists())
        context = self.root / "context"
        context.mkdir()
        Proposer(adapter.spec.channels["proposer"]).call("mission", context, "improver")
        record = self.launch_record("claude-login improver")
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
                "claude-sonnet-5-5",
                "--setting-sources",
                "project,local",
                "--strict-mcp-config",
                "--no-session-persistence",
            ],
        )
        for record in self.records():
            self.assertNotIn("CLAUDE_CONFIG_DIR", record["env"])
            self.assertEqual(record["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "fixture-oauth")
            self.assertTrue(all(key not in record["env"] for key in providers.STRIP_ENV))
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN")
        adapter.execute("one", None, "native-login")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.records()[-1]["env"])
        self.assertNotIn("CLAUDE_CONFIG_DIR", self.records()[-1]["env"])

    def test_chatgpt_subscription_executor_and_improver_launches(self):
        adapter = self.adapter(
            **{
                "executor.name": "codex",
                "executor.model": "chatgpt-login/gpt-6.1-sol",
                "improver.channel": "codex",
                "improver.model": "chatgpt-login/gpt-6.1-sol",
            }
        )
        home = self.user_home / ".codex"
        before = {p.name: p.read_bytes() for p in home.iterdir()}
        result = adapter.execute("one", None, "codex")
        record = self.launch_record("chatgpt-login executor")
        self.assertEqual(
            record["argv"],
            [
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "-m",
                "gpt-6.1-sol",
                "-c",
                'model_reasoning_effort="high"',
                "-C",
                record["cwd"],
                "-",
            ],
        )
        self.assertFalse((result / "codex-home").exists())
        context = self.root / "context"
        context.mkdir()
        Proposer(adapter.spec.channels["proposer"]).call("mission", context, "improver")
        record = self.launch_record("chatgpt-login improver")
        self.assertEqual(
            record["argv"],
            [
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "-m",
                "gpt-6.1-sol",
                "-c",
                'model_reasoning_effort="high"',
                "-C",
                record["cwd"],
                "-",
            ],
        )
        for record in self.records():
            self.assertEqual(
                record["env"]["CODEX_HOME"], str(self.root / "state/harness2/codex-home")
            )
            self.assertFalse(record["config_exists"])
            self.assertFalse(record["agents_exists"])
            self.assertFalse(record["hooks_exists"])
            self.assertTrue(
                all(
                    key not in record["env"]
                    for key in (*providers.STRIP_ENV, "OPENAI_API_KEY", "CODEX_API_KEY")
                )
            )
        self.assertEqual({p.name: p.read_bytes() for p in home.iterdir()}, before)
        self.assertFalse((settings.state_dir() / "codex-home").exists())

    def test_configured_chatgpt_home_is_used_without_writes(self):
        for populated in (False, True):
            with self.subTest(populated=populated):
                home = self.user_home / f"custom codex-{populated}"
                if populated:
                    home.mkdir()
                    for name in ("auth.json", "config.toml", "AGENTS.md", "hooks.json"):
                        (home / name).write_text("fixture " + name)
                before = {p.name: p.read_bytes() for p in home.iterdir()} if populated else {}
                adapter = self.adapter(
                    f"custom-{populated}",
                    **{
                        "executor.name": "codex",
                        "executor.model": "chatgpt-login/model",
                        "improver.channel": "codex",
                        "improver.model": "chatgpt-login/model",
                        "providers.chatgpt-login.home": f"~/{home.name}",
                    },
                )
                with patch.dict(os.environ, {"CODEX_HOME": str(self.user_home / ".codex")}):
                    result = adapter.execute("one", None, "custom")
                    self.assertFalse((result / "codex-home").exists())
                    context = self.root / f"context-{populated}"
                    context.mkdir()
                    Proposer(adapter.spec.channels["proposer"]).call("mission", context, "custom")
                for record in self.records()[-2:]:
                    self.assertEqual(record["env"]["CODEX_HOME"], str(home))
                    self.assertEqual(record["config_exists"], populated)
                    self.assertEqual(record["agents_exists"], populated)
                    self.assertEqual(record["hooks_exists"], populated)
                    self.assertNotIn("OPENAI_API_KEY", record["env"])
                    self.assertNotIn("CODEX_API_KEY", record["env"])
                    self.assertIn("--ignore-user-config", record["argv"])
                    self.assertIn("--ignore-rules", record["argv"])
                    self.assertIn("--ephemeral", record["argv"])
                after = {p.name: p.read_bytes() for p in home.iterdir()} if home.exists() else {}
                self.assertEqual(after, before)
                self.assertEqual(home.exists(), populated)

    def test_inherited_claude_home_is_preserved_and_codex_home_is_overridden(self):
        context = self.root / "context"
        context.mkdir()
        for kind, provider_name, variable in (
            ("claude", "claude-login", "CLAUDE_CONFIG_DIR"),
            ("codex", "chatgpt-login", "CODEX_HOME"),
        ):
            with (
                self.subTest(kind=kind),
                patch.dict(os.environ, {variable: str(self.user_home / ("." + kind))}),
            ):
                adapter = self.adapter(
                    kind,
                    **{
                        "executor.name": kind,
                        "executor.model": provider_name + "/model",
                        "improver.channel": kind,
                        "improver.model": provider_name + "/model",
                    },
                )
                adapter.execute("one", None, "inherited")
                expected = (
                    os.environ[variable]
                    if kind == "claude"
                    else str(settings.state_dir() / "codex-home")
                )
                self.assertEqual(self.records()[-1]["env"][variable], expected)
                Proposer(adapter.spec.channels["proposer"]).call("mission", context, "inherited")
                self.assertEqual(self.records()[-1]["env"][variable], expected)

    def test_login_status_success_failure_missing_cli_and_timeout(self):
        for kind, argv, login in (
            ("claude-login", ["auth", "status"], "claude auth login"),
            ("chatgpt-login", ["login", "status"], "harness2 login codex"),
        ):
            with self.subTest(provider=kind):
                provider = providers.Provider(kind, kind)
                self.assertEqual(providers.check(provider), [])
                record = self.records()[-1]
                self.assertEqual(record["argv"], argv)
                self.assertNotIn("CLAUDE_CONFIG_DIR", record["env"])
                if kind == "chatgpt-login":
                    self.assertEqual(
                        record["env"]["CODEX_HOME"], str(settings.state_dir() / "codex-home")
                    )
                    self.assertNotIn("OPENAI_API_KEY", record["env"])
                    self.assertNotIn("CODEX_API_KEY", record["env"])
                else:
                    self.assertNotIn("CODEX_HOME", record["env"])
                self.assertTrue(all(key not in record["env"] for key in providers.STRIP_ENV))
                where = " for harness2" if kind == "chatgpt-login" else ""
                problem = [f"{kind}: not logged in{where}; run `{login}`"]
                with patch.dict(os.environ, {"FAKE_LOGIN_RC": "1"}):
                    self.assertEqual(providers.check(provider), problem)
                with patch.dict(os.environ, {"PATH": ""}):
                    self.assertEqual(providers.check(provider), problem)
                with patch(
                    "harness2.providers.subprocess.run",
                    side_effect=subprocess.TimeoutExpired("cli", 10),
                ):
                    self.assertEqual(providers.check(provider), problem)
        args = providers.codex_args(
            providers.Provider("chatgpt-login", "chatgpt-login"),
            'model"\nnext\x7f',
            'effort"\nnext',
        )
        self.assertEqual(args[3], 'model"\nnext\x7f')
        self.assertEqual(tomllib.loads(args[5]), {"model_reasoning_effort": 'effort"\nnext'})

    def test_codex_logged_in_checks_only_the_requested_home(self):
        home = self.root / "login home"
        with patch.dict(os.environ, {"CODEX_HOME": str(self.user_home / ".codex")}):
            self.assertTrue(providers.codex_logged_in(home))
        record = self.records()[-1]
        self.assertEqual(record["argv"], ["login", "status"])
        self.assertEqual(record["env"]["CODEX_HOME"], str(home))
        self.assertFalse(record["config_exists"])
        self.assertFalse(record["agents_exists"])
        self.assertFalse(record["hooks_exists"])
        self.assertTrue(
            all(
                key not in record["env"]
                for key in (*providers.STRIP_ENV, "OPENAI_API_KEY", "CODEX_API_KEY")
            )
        )
        with patch.dict(os.environ, {"FAKE_LOGIN_RC": "1"}):
            self.assertFalse(providers.codex_logged_in(home))
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("codex", 10), ValueError()):
            with (
                self.subTest(error=type(error).__name__),
                patch("harness2.providers.subprocess.run", side_effect=error) as run,
            ):
                self.assertFalse(providers.codex_logged_in(home))
                self.assertEqual(run.call_args.kwargs["timeout"], 10)
        self.assertFalse(home.exists())

    def test_api_provider_homes_and_ambient_oauth_are_unchanged(self):
        for kind, provider_name in (("claude", "anthropic"), ("codex", "openai")):
            with self.subTest(kind=kind):
                adapter = self.adapter(
                    kind,
                    **{
                        "executor.name": kind,
                        "executor.model": provider_name + "/fixture",
                        "improver.channel": kind,
                        "improver.model": provider_name + "/fixture",
                    },
                )
                result = adapter.execute("one", None, "api")
                record = self.records()[-1]
                self.assertEqual(record["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "fixture-oauth")
                if kind == "claude":
                    self.assertEqual(record["env"]["ANTHROPIC_API_KEY"], "ambient-anthropic")
                else:
                    self.assertEqual(record["env"]["OPENAI_API_KEY"], "ambient-openai")
                    self.assertEqual(record["env"]["CODEX_HOME"], str(result / "codex-home"))
                    self.assertEqual(
                        record["config"],
                        providers.codex_config(
                            adapter.settings.provider("openai"), "fixture", "high"
                        ),
                    )
                    self.assertNotIn("-c", record["argv"])
                Proposer(adapter.spec.channels["proposer"]).call("mission", None, "api-improver")
                record = self.records()[-1]
                self.assertEqual(record["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "fixture-oauth")
                if kind == "codex":
                    self.assertEqual(
                        record["config"],
                        providers.codex_config(
                            adapter.settings.provider("openai"), "fixture", "high"
                        ),
                    )
                    self.assertNotEqual(record["env"]["CODEX_HOME"], str(self.user_home / ".codex"))

    def test_wrong_cli_and_bridge_reject_subscription_providers(self):
        claude = providers.Provider("claude-login", "claude-login")
        chatgpt = providers.Provider("chatgpt-login", "chatgpt-login")
        with self.assertRaisesRegex(ConfigError, "cannot use a ChatGPT login"):
            providers.claude_env(chatgpt)
        with self.assertRaises(ConfigError):
            providers.codex_config(claude, "model", "high")
        with self.assertRaises(ConfigError):
            providers.codex_args(claude, "model", "high")
        for provider in (claude, chatgpt):
            for operation in (providers.opencode_config, providers.litellm_params):
                with self.subTest(provider=provider.type, operation=operation.__name__):
                    with self.assertRaisesRegex(ConfigError, "API provider"):
                        operation(provider, "model")

    def test_doctor_per_role_subscription_compatibility(self):
        for channel, provider_name in (
            ("claude", "claude-login"),
            ("codex", "chatgpt-login"),
            ("claude", "chatgpt-login"),
            ("codex", "claude-login"),
            ("opencode", "claude-login"),
            ("opencode", "chatgpt-login"),
        ):
            with self.subTest(channel=channel, provider=provider_name):
                adapter = (
                    self.adapter(
                        **{
                            "executor.name": channel,
                            "executor.model": provider_name + "/model",
                            "improver.channel": channel,
                            "improver.model": provider_name + "/model",
                        }
                    )
                    if channel != "opencode"
                    else self.adapter(
                        **{
                            "improver.channel": channel,
                            "improver.model": provider_name + "/model",
                        }
                    )
                )
                code, roles = self.doctor(adapter)
                compatible = (channel, provider_name) in (
                    ("claude", "claude-login"),
                    ("codex", "chatgpt-login"),
                )
                self.assertEqual(code, 0 if compatible else 1)
                self.assertEqual(roles["improver"]["ok"], compatible)
                self.assertEqual(roles["judge"]["ok"], compatible)

    def doctor(self, adapter):
        output = io.StringIO()
        argv = ["-C", str(self.project), "--json"]
        for key, value in adapter.settings.flat().items():
            if key.startswith(("executor.", "improver.", "providers.")) and not isinstance(
                value, list
            ):
                argv += ["-c", f"{key}={json.dumps(value)}"]
        with contextlib.redirect_stdout(output):
            code = main([*argv, "doctor"])
        return code, {row["check"]: row for row in json.loads(output.getvalue())}

    def test_doctor_login_status_uses_dedicated_codex_home(self):
        adapter = self.adapter(
            **{
                "executor.name": "codex",
                "executor.model": "chatgpt-login/model",
                "improver.channel": "codex",
                "improver.model": "chatgpt-login/model",
            }
        )
        code, rows = self.doctor(adapter)
        self.assertEqual(code, 0)
        self.assertNotIn("Codex home", rows)
        with patch.dict(os.environ, {"FAKE_LOGIN_RC": "1"}):
            code, rows = self.doctor(adapter)
            self.assertEqual(code, 1)
            self.assertEqual(
                rows["provider chatgpt-login"]["detail"],
                "chatgpt-login: not logged in for harness2; run `harness2 login codex`",
            )
            code, rows = self.doctor(self.adapter())
            self.assertEqual(code, 1)
            self.assertEqual(
                rows["provider claude-login"]["detail"],
                "claude-login: not logged in; run `claude auth login`",
            )
        _, rows = self.doctor(self.adapter())
        self.assertNotIn("Codex home", rows)
        self.assertFalse((settings.state_dir() / "codex-home").exists())
        self.assertTrue(
            all(
                record["argv"] in (["auth", "status"], ["login", "status"])
                for record in self.records()
            )
        )

    def test_doctor_custom_codex_home_login_and_instruction_files(self):
        home = self.user_home / "custom-codex"
        home.mkdir()
        adapter = self.adapter(
            **{
                "executor.name": "codex",
                "executor.model": "chatgpt-login/model",
                "improver.channel": "codex",
                "improver.model": "chatgpt-login/model",
                "providers.chatgpt-login.home": "~/custom-codex",
            }
        )
        with patch.dict(os.environ, {"FAKE_LOGIN_RC": "1"}):
            code, rows = self.doctor(adapter)
        self.assertEqual(code, 1)
        self.assertEqual(
            rows["provider chatgpt-login"]["detail"],
            f"chatgpt-login: not logged in at {home}; run `codex login` with CODEX_HOME={home}",
        )
        self.assertEqual(self.records()[-1]["env"]["CODEX_HOME"], str(home))
        for files in ((), ("hooks.json",), ("AGENTS.md",), ("AGENTS.md", "hooks.json")):
            with self.subTest(files=files):
                for name in ("AGENTS.md", "hooks.json"):
                    (home / name).write_text("fixture" if name in files else "")
                before = {p.name: p.read_bytes() for p in home.iterdir()}
                code, rows = self.doctor(adapter)
                self.assertEqual(code, 0)
                if files:
                    self.assertEqual(
                        rows["Codex home"],
                        {
                            "check": "Codex home",
                            "ok": True,
                            "detail": " and ".join(str(home / name) for name in files)
                            + " are part of every Codex run",
                        },
                    )
                else:
                    self.assertNotIn("Codex home", rows)
                self.assertEqual({p.name: p.read_bytes() for p in home.iterdir()}, before)

    def test_executor_usage_limits_stop_and_resume_entire_run(self):
        adapter = self.adapter()
        self.stop_and_resume(adapter, "executor")

    def test_improver_usage_limits_stop_without_retry_and_resume_entire_run(self):
        adapter = self.adapter()
        self.stop_and_resume(adapter, "improver")

    def stop_and_resume(self, adapter, role):
        run_root = adapter.spec.runs_root
        event_path = run_root / "events.jsonl"
        prop = Proposer(adapter.spec.channels["proposer"], audit_dir=run_root / "audit")
        kwargs = dict(
            adapter=adapter,
            prop=prop,
            runs_root=run_root,
            library_root=run_root / "library",
            modes=("agg",),
        )
        events.start(event_path)
        with (
            patch.dict(os.environ, {"LIMIT_ROLE": role}),
            patch("harness2.core.proposer.time", wraps=time) as clock,
        ):
            with self.assertRaisesRegex(events.Stopped, "^usage_limit$"):
                schedule.run_sweep(1, **kwargs)
            clock.sleep.assert_not_called()
        self.assertEqual(events.totals()["stopped"], "usage_limit")
        self.assertFalse(list(run_root.glob("tasks-*/**/summary.json")))
        if role == "executor":
            self.assertEqual(list((run_root / "rollouts").iterdir()), [])
            self.assertEqual(events.totals()["calls"], 0)
        else:
            self.assertFalse(list((run_root / "audit").glob("*.response.md")))
        events.start(event_path)
        resumed = schedule.run_sweep(1, **kwargs)
        self.assertEqual((resumed["domains"][0]["done"], resumed["domains"][0]["failed"]), (1, 0))
        self.assertTrue(list(run_root.glob("tasks-*/**/summary.json")))
        rollouts = list((run_root / "rollouts").iterdir())
        self.assertTrue(rollouts)
        self.assertTrue(all(runmark.is_complete(path, match_harness=False) for path in rollouts))

    def test_usage_limit_failure_sources_and_success_mentions(self):
        for source in ("event", "stderr", "stdout"):
            for kind in ("claude", "codex"):
                with self.subTest(source=source, kind=kind):
                    provider_name = "claude-login" if kind == "claude" else "chatgpt-login"
                    adapter = self.adapter(
                        f"{kind}-{source}",
                        **{
                            "executor.name": kind,
                            "executor.model": provider_name + "/model",
                        },
                    )
                    events.start(None)
                    with patch.dict(os.environ, {"LIMIT_ROLE": "executor", "LIMIT_SOURCE": source}):
                        with self.assertRaisesRegex(events.Stopped, "usage_limit"):
                            adapter.execute("one", None, "limited")
                    self.assertEqual(list((adapter.spec.runs_root / "rollouts").iterdir()), [])
                    events.start(None)
                    provider = adapter.settings.provider(provider_name)
                    with patch.dict(os.environ, {"LIMIT_ROLE": "improver", "LIMIT_SOURCE": source}):
                        context = self.root / "context"
                        context.mkdir(exist_ok=True)
                        with self.assertRaisesRegex(events.Stopped, "usage_limit"):
                            Proposer(
                                ChannelSpec(kind, provider_name + "/model", provider=provider)
                            ).call("mission", context, "limited")
        for text in (
            "usage limit",
            "RATE_LIMIT",
            "too many requests",
            "HTTP 429",
            "quota",
            "credit balance is too low",
        ):
            self.assertIsNotNone(providers.USAGE_LIMIT.search(text))
        self.assertIsNone(providers.USAGE_LIMIT.search("14290"))
        events.start(None)
        adapter = self.adapter("success")
        (self.root / "bin/claude").write_text(
            f"#!{sys.executable}\nimport json\n"
            "print(json.dumps({'type':'result', 'subtype':'success', 'result':'rate limit fixed'}))\n"
        )
        self.assertTrue(
            runmark.is_complete(
                adapter.execute("one", None, "success"), harness_dir=adapter.base_harness()
            )
        )
        self.assertTrue(
            Proposer(adapter.spec.channels["proposer"]).call("mission", None, "success")
        )

    def test_core_does_not_swallow_stopped_from_batched_adapter(self):
        adapter = self.adapter()
        adapter.execute_many = Mock(side_effect=events.Stopped("usage_limit"))
        with self.assertRaisesRegex(events.Stopped, "usage_limit"):
            schedule._roll_bases("one", adapter, [("a", "a"), ("b", "b")], self.root / "dead")
        with patch.object(adapter, "execute", side_effect=events.Stopped("usage_limit")):
            with self.assertRaisesRegex(events.Stopped, "usage_limit"):
                recursion._roll(adapter, None, "one", "a", None, lambda label: label, self.root)

    def test_opencode_usage_limit_stops_even_with_a_partial_answer(self):
        binary = self.root / "bin/opencode"
        binary.write_text(
            f"#!{sys.executable}\nimport json\nfrom pathlib import Path\n"
            "Path('output/answer.md').write_text('partial')\n"
            "print(json.dumps({'type':'error','error':{'data':{'message':'quota exceeded'}}}))\n"
        )
        binary.chmod(0o755)
        provider = providers.Provider("api", "openai")
        prop = Proposer(ChannelSpec("opencode", "api/model", provider=provider))
        with patch("harness2.core.proposer.time", wraps=time) as clock:
            with self.assertRaisesRegex(events.Stopped, "usage_limit"):
                prop.call("mission", None, "limit")
            clock.sleep.assert_not_called()
        self.assertEqual(events.totals()["stopped"], "usage_limit")

    def test_project_workspace_prunes_before_adding(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        workspace.commit_all(self.project, "fixture")
        with patch("harness2.workspace.subprocess.run", wraps=subprocess.run) as run:
            with workspace.project_workspace(self.project, "HEAD") as ws:
                self.assertTrue(ws.exists())
        argv = [call.args[0] for call in run.call_args_list]
        self.assertEqual(argv[0], ["git", "-C", str(self.project), "worktree", "prune"])
        self.assertEqual(argv[1][4], "add")
