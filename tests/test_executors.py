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

"""Offline executor contracts, native-file safety and documented Claude stream shapes."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness2 import providers
from harness2.core import prompts
from harness2.core import tree as HT
from harness2.core.spec import INSTALL_MODES, BenchPrompts
from harness2.settings import ConfigError
from harness2.substrate import (
    CLAUDE_SURFACE,
    CODEX_SURFACE,
    OPENCODE_SURFACE,
    ClaudeSubstrate,
    CodexSubstrate,
    CommandSubstrate,
    OpencodeSubstrate,
)
from harness2.substrate.claude import parse_events


class SurfaceTests(unittest.TestCase):
    def test_script_dir_and_references(self):
        for surface in (CLAUDE_SURFACE, CODEX_SURFACE, OPENCODE_SURFACE):
            with self.subTest(surface=surface.name):
                script = f"{surface.script_dir}/helper.py"
                self.assertTrue(HT.is_allowed(surface, script))
                if surface.script_dir != "scripts":
                    self.assertFalse(HT.is_allowed(surface, "scripts/helper.py"))
                tree = {surface.always_on[0]: f"python3 {script}", script: "print('ok')"}
                HT.validate(surface, tree)
                del tree[script]
                with self.assertRaisesRegex(ValueError, "but that file is not in the harness"):
                    HT.validate(surface, tree)
                tree = {
                    surface.always_on[0]: "helper",
                    f"{surface.skill_dir}/helper/SKILL.md": f"---\nname: helper\ndescription: Help\n---\npython3 {surface.skill_dir}/helper/scripts/tool.py",
                }
                with self.assertRaisesRegex(ValueError, "tool.py.*not in the harness"):
                    HT.validate(surface, tree)
                tree[f"{surface.skill_dir}/helper/scripts/tool.py"] = "pass"
                HT.validate(surface, tree)

    def test_skills_can_be_discovered_without_trigger(self):
        tree = {".claude/skills/helper/SKILL.md": "---\ndescription: Help\n---\nDo it."}
        before = dict(tree)
        HT.validate(CLAUDE_SURFACE, tree)
        self.assertEqual(HT.autotrigger(CLAUDE_SURFACE, tree), [])
        self.assertEqual(tree, before)
        required = replace(CLAUDE_SURFACE, skills_need_trigger=True)
        with self.assertRaisesRegex(ValueError, "never named"):
            HT.validate(required, tree)
        tree[".claude/skills/helper/SKILL.md"] = "missing description"
        with self.assertRaisesRegex(ValueError, "description"):
            HT.validate(CLAUDE_SURFACE, tree)

    def test_globs_override_component_rules_but_keep_caps_and_compile(self):
        substrate = CommandSubstrate("true", ("custom/*",))
        surface = substrate.surface()
        self.assertEqual(substrate.NATIVE_PATHS, (":(glob)custom/*",))
        self.assertTrue(HT.is_allowed(surface, "custom/a.txt"))
        self.assertFalse(HT.is_allowed(surface, "sub_agents/helper/AGENT.md"))
        self.assertFalse(HT.is_allowed(surface, "../custom/a.txt"))
        HT.validate(surface, {"custom/hook.json": "bad json", "ignored.py": "bad syntax!"})
        with self.assertRaisesRegex(ValueError, "does not compile"):
            HT.validate(surface, {"custom/tool.py": "bad syntax!"})
        special = replace(surface, editable_globs=("**",), hooks_file="hooks.json")
        HT.validate(
            special,
            {"sub_agents/a/AGENT.md": "no description", "hooks.json": "bad"},
            allow_subagents=False,
        )
        capped = replace(surface, always_on=("custom/a.txt",), always_on_max=2)
        with self.assertRaisesRegex(ValueError, "over the 2 cap"):
            HT.validate(capped, {"custom/a.txt": "long"})
        with self.assertRaises(ValueError):
            CommandSubstrate("true", ())

    def test_new_install_modes_and_all_surface_prompts(self):
        self.assertIn("claude_root", INSTALL_MODES)
        self.assertIn("command_files", INSTALL_MODES)
        for surface in (
            CLAUDE_SURFACE,
            CODEX_SURFACE,
            OPENCODE_SURFACE,
            CommandSubstrate("true", ("*.md",)).surface(),
        ):
            with self.subTest(surface=surface.name):
                text = prompts.propose(BenchPrompts(work="x"), "INV", surface)
                self.assertNotIn("__", text)

    def test_glob_surface_accepts_empty_directory_as_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = CommandSubstrate("true", ("custom/*",)).surface()
            self.assertTrue(HT.is_tree(command, root))
            self.assertFalse(HT.is_tree(command, root / "missing"))
            file = root / "file.txt"
            file.write_text("content")
            self.assertFalse(HT.is_tree(command, file))
            for surface in (CLAUDE_SURFACE, CODEX_SURFACE, OPENCODE_SURFACE):
                self.assertFalse(HT.is_tree(surface, root))
                always_on = root / surface.always_on[0]
                always_on.write_text("")
                self.assertTrue(HT.is_tree(surface, root))
                always_on.unlink()


class NativeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def native_project(self, substrate):
        surface = substrate.surface()
        self.write(surface.always_on[0], "Use helper.")
        self.write(f"{surface.skill_dir}/helper/SKILL.md", "---\ndescription: Help\n---\nBody")
        self.write(f"{surface.skill_dir}/helper/scripts/tool.py", "pass\n")
        self.write(f"{surface.skill_dir}/helper/reference.md", "keep reference")
        self.write(f"{surface.script_dir}/tool.py", "pass\n")
        self.write(f"{surface.script_dir}/extra.txt", "keep extra")
        hooks = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}}
        if surface.name == "claude":
            self.write(
                ".claude/settings.json", json.dumps({"permissions": {"allow": ["Read"]}, **hooks})
            )
            self.write(".claude/agents/helper.md", "---\ndescription: Help\n---\nDo it.")
            self.write(".claude/agents/notes.txt", "keep notes")
        else:
            self.write(surface.hooks_file, json.dumps(hooks))
            self.write(".codex/config.toml", "keep = true")
        return hooks

    def test_claude_round_trip_hooks_merge_and_bounded_deletions(self):
        sub = ClaudeSubstrate()
        hooks = self.native_project(sub)
        rejected = []
        tree = sub.import_tree(self.root, rejected=rejected)
        self.assertEqual(
            set(rejected),
            {
                ".claude/skills/helper/reference.md",
                ".claude/scripts/extra.txt",
                ".claude/agents/notes.txt",
            },
        )
        self.assertIn("sub_agents/helper/AGENT.md", tree)
        self.assertEqual(json.loads(tree[".claude/hooks.json"]), hooks)
        dest = self.root / "destination"
        settings = dest / ".claude/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"model": "keep"}')
        result = sub.export_tree(tree, dest, base={})
        self.assertIn(".claude/agents/helper.md", result["written"])
        self.assertIn("name: helper", (dest / ".claude/agents/helper.md").read_text())
        self.assertEqual(json.loads(settings.read_text()), {"model": "keep", **hooks})
        imported = sub.import_tree(dest)
        self.assertEqual(
            sub.export_tree(imported, dest, base=imported), {"written": [], "deleted": []}
        )
        self.assertEqual(tree.keys(), imported.keys())
        changed = {k: v for k, v in tree.items() if k not in (".claude/hooks.json", "CLAUDE.md")}
        result = sub.export_tree(changed, self.root, base=tree)
        self.assertEqual(result["deleted"], ["CLAUDE.md"])
        self.assertIn(".claude/settings.json", result["written"])
        self.assertEqual(
            json.loads((self.root / ".claude/settings.json").read_text()),
            {"permissions": {"allow": ["Read"]}},
        )
        self.assertEqual(
            (self.root / ".claude/skills/helper/reference.md").read_text(), "keep reference"
        )

    def test_claude_agent_name_inserted_only_when_missing(self):
        sub = ClaudeSubstrate()
        for name, body in (
            ("present", "---\nname: present\ndescription: Help\n---\nBody"),
            ("plain", "Body"),
            ("empty", "---\nname: \ndescription: Help\n---\nBody"),
            ("header", "---\n---\nBody"),
        ):
            sub.export_tree({f"sub_agents/{name}/AGENT.md": body}, self.root, base={})
            exported = (self.root / f".claude/agents/{name}.md").read_text()
            self.assertIn(f"name: {name}\n", exported)
            self.assertEqual(sum(line.startswith("name:") for line in exported.splitlines()), 1)
            if name == "present":
                self.assertEqual(exported, body)

    def test_codex_round_trip_no_stubs_or_subagents_and_bounded_deletions(self):
        sub = CodexSubstrate()
        self.native_project(sub)
        rejected = []
        tree = sub.import_tree(self.root, rejected=rejected)
        self.assertIn(".agents/skills/helper/reference.md", rejected)
        dest = self.root / "destination"
        candidate = {
            **tree,
            "sub_agents/helper/AGENT.md": "description: Help",
            **dict(CODEX_SURFACE.stubs),
        }
        result = sub.export_tree(candidate, dest, base={})
        self.assertEqual(sub.import_tree(dest), tree)
        self.assertFalse((dest / "sub_agents").exists())
        self.assertFalse(any(dest.rglob("README.md")))
        self.assertEqual(set(result["written"]), set(tree))
        self.assertEqual(sub.export_tree(tree, dest, base=tree), {"written": [], "deleted": []})
        result = sub.export_tree({}, self.root, base=tree)
        self.assertEqual(set(result["deleted"]), set(tree))
        self.assertTrue((self.root / ".codex/config.toml").is_file())
        self.assertTrue((self.root / ".agents/skills/helper/reference.md").is_file())

    def test_strip_native_preserves_unmanaged_files_and_settings(self):
        for sub in (ClaudeSubstrate(), CodexSubstrate()):
            with self.subTest(substrate=sub.name):
                self.native_project(sub)
                sub.strip_native(self.root)
                self.assertEqual(sub.import_tree(self.root), {})
                surface = sub.surface()
                self.assertTrue((self.root / f"{surface.skill_dir}/helper/reference.md").is_file())
                self.assertTrue((self.root / f"{surface.script_dir}/extra.txt").is_file())
                if sub.name == "claude":
                    self.assertIn(
                        "permissions", json.loads((self.root / ".claude/settings.json").read_text())
                    )
                    self.assertTrue((self.root / ".claude/agents/notes.txt").is_file())

    def test_materialise_and_install_native_layout(self):
        for sub, mode in (
            (ClaudeSubstrate(), "claude_root"),
            (CodexSubstrate(), "codex_root"),
            (CommandSubstrate("true", ("custom/*",)), "command_files"),
        ):
            with self.subTest(mode=mode):
                canonical = self.root / mode
                surface = sub.surface()
                if mode == "command_files":
                    tree = {"custom/file.txt": "custom"}
                else:
                    tree = {
                        surface.always_on[0]: "instructions",
                        f"{surface.script_dir}/tool.py": "pass",
                    }
                    if mode == "claude_root":
                        tree["sub_agents/helper/AGENT.md"] = "---\ndescription: Help\n---\nBody"
                        tree[".claude/hooks.json"] = '{"hooks":{"Stop":[]}}'
                HT.write_tree(surface, tree, canonical)
                (canonical / "unmanaged.txt").write_text("do not stage")
                install = sub.materialise(canonical, SimpleNamespace(install_mode=mode), {})
                self.addCleanup(shutil.rmtree, install.staging_dir, True)
                self.assertTrue(install.fingerprint)
                self.assertFalse((install.staging_dir / "unmanaged.txt").exists())
                workspace = self.root / f"{mode}-workspace"
                if mode == "claude_root":
                    (workspace / ".claude").mkdir(parents=True)
                    (workspace / ".claude/settings.json").write_text('{"model":"keep"}')
                sub.install_into(install, workspace)
                imported = sub.import_tree(workspace)
                self.assertEqual(set(imported), set(tree))
                if mode == "claude_root":
                    self.assertIn("name: helper", imported["sub_agents/helper/AGENT.md"])
                    self.assertEqual(
                        json.loads((workspace / ".claude/settings.json").read_text())["model"],
                        "keep",
                    )
                else:
                    self.assertEqual(imported, tree)
                with self.assertRaises(ValueError):
                    sub.materialise(canonical, SimpleNamespace(install_mode="wrong"), {})

    def test_native_exports_reject_unmanaged_paths_and_symlinks_before_writes(self):
        external = self.write("external.txt", "keep")
        (self.root / "CLAUDE.md").symlink_to(external)
        with self.assertRaisesRegex(ConfigError, "symlink"):
            ClaudeSubstrate().export_tree({"CLAUDE.md": "overwrite"}, self.root, base={})
        self.assertEqual(external.read_text(), "keep")
        with self.assertRaisesRegex(ConfigError, "symlink"):
            ClaudeSubstrate().import_tree(self.root)
        (self.root / "CLAUDE.md").unlink()
        (self.root / ".agents").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ConfigError, "symlink"):
            CodexSubstrate().export_tree(
                {"AGENTS.md": "new", ".agents/scripts/tool.py": "pass"}, self.root, base={}
            )
        self.assertFalse((self.root / "AGENTS.md").exists())
        for sub in (ClaudeSubstrate(), CodexSubstrate(), CommandSubstrate("true", ("*.md",))):
            with self.subTest(substrate=sub.name):
                with self.assertRaises(ValueError):
                    sub.export_tree({"unmanaged.txt": "bad"}, self.root, base={})
                with self.assertRaises(ValueError):
                    sub.export_tree({"../escape.md": "bad"}, self.root, base={})

    def test_malformed_settings_and_hooks_never_overwrite_data(self):
        settings = self.write(".claude/settings.json", '{"broken":')
        sub = ClaudeSubstrate()
        for text in ('{"broken":', "[]"):
            settings.write_text(text)
            with self.assertRaises(ConfigError):
                sub.export_tree(
                    {"CLAUDE.md": "new", ".claude/hooks.json": '{"hooks":{}}'}, self.root, base={}
                )
            self.assertEqual(settings.read_text(), text)
            self.assertFalse((self.root / "CLAUDE.md").exists())
        settings.write_text('{"hooks":[]}')
        with self.assertRaises(ConfigError):
            sub.import_tree(self.root)
        for content in ("bad", "[]", '{"hooks":[]}'):
            with self.assertRaises(ConfigError):
                sub.export_tree({".claude/hooks.json": content}, self.root, base={})

    def test_command_native_files_and_run_with_quoted_paths(self):
        sub = CommandSubstrate(
            'cat {prompt_file}; printf "diagnostic" >&2; '
            "mkdir -p {output}; cp {prompt_file} {output}/answer.txt; "
            'printf "%s" {workspace} > {output}/workspace.txt',
            ("custom/*",),
        )
        self.write("custom/rules.txt", "rules")
        self.write("keep.txt", "keep")
        base = sub.import_tree(self.root)
        self.assertEqual(base, {"custom/rules.txt": "rules"})
        sub.strip_native(self.root)
        self.assertTrue((self.root / "keep.txt").exists())
        self.assertFalse((self.root / "custom/rules.txt").exists())
        sub.export_tree(base, self.root, base={})
        prompt = self.write("prompt ' $file.md", "hello")
        workspace = self.root / "work ' $dir"
        workspace.mkdir()
        run_dir = self.root / "run"
        self.assertEqual(sub.run(prompt, workspace, run_dir, 5, os.environ), 0)
        self.assertEqual((workspace / "output/answer.txt").read_text(), "hello")
        self.assertEqual((workspace / "output/workspace.txt").read_text(), str(workspace))
        stream = run_dir / "stream.jsonl"
        self.assertEqual(sub.final_message(stream), "hellodiagnostic")
        self.assertEqual(
            sub.read_trajectory(stream, obs_trunc=10, max_chars=100), "[OBSERVATION] diagnostic"
        )
        self.assertEqual(sub.cost_usd(stream), 0.0)
        self.assertEqual(
            CommandSubstrate("exit 7", ("*.md",)).run(prompt, workspace, run_dir, 5, os.environ), 7
        )
        with self.assertRaises(subprocess.TimeoutExpired):
            CommandSubstrate("sleep 2", ("*.md",)).run(prompt, workspace, run_dir, 0.01, os.environ)


class ClaudeStreamTests(unittest.TestCase):
    fixture = Path(__file__).parent / "fixtures/claude-stream.jsonl"

    def test_documented_message_and_result_fields(self):
        sub = ClaudeSubstrate()
        rendered = sub.read_trajectory(self.fixture, obs_trunc=1000, max_chars=10000)
        for text in (
            "[HARNESS] init | model=claude-fixture cwd=/workspace",
            "[REASONING] Inspect the input first.",
            "[ASSISTANT] I will inspect the input.",
            '[ACTION] Bash: {"command": "cat input.txt"}',
            "[OBSERVATION] input contents",
            "[OBSERVATION ERROR] File does not exist.",
        ):
            self.assertIn(text, rendered)
        self.assertEqual(sub.final_message(self.fixture), "Task complete.")
        self.assertAlmostEqual(sub.cost_usd(self.fixture), 0.0123)
        clipped = sub.read_trajectory(self.fixture, obs_trunc=5, max_chars=10000, args_trunc=5)
        self.assertIn("…(+", clipped)
        self.assertIn("render truncation)", clipped)
        capped = sub.read_trajectory(self.fixture, obs_trunc=1000, max_chars=25)
        self.assertTrue(capped.startswith(rendered[:25]))
        self.assertIn("render truncation)", capped)

    def test_errors_fallback_junk_and_missing_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stream.jsonl"
            path.write_text(
                'junk\n[]\n{"type":\n'
                + json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "fallback"}]},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "result",
                        "subtype": "error_during_execution",
                        "is_error": True,
                        "errors": ["failed to execute"],
                        "total_cost_usd": 0.1,
                    }
                )
                + "\n"
            )
            sub = ClaudeSubstrate()
            self.assertEqual(len(parse_events(path)), 2)
            self.assertEqual(sub.final_message(path), "fallback")
            self.assertIn(
                "[ERROR] failed to execute",
                sub.read_trajectory(path, obs_trunc=100, max_chars=1000),
            )
            path.write_text("junk\n{}\n")
            self.assertEqual(sub.final_message(path), "")
            self.assertEqual(sub.cost_usd(path), 0.0)
            self.assertEqual(
                sub.read_trajectory(path, obs_trunc=100, max_chars=1000),
                "(no parseable events in trajectory)",
            )
            path.unlink()
            self.assertIn(
                "failed to read trajectory",
                sub.read_trajectory(path, obs_trunc=100, max_chars=1000),
            )
            self.assertEqual(sub.final_message(path), "")
            self.assertEqual(sub.cost_usd(path), 0.0)


class CliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.capture = self.root / "capture.json"
        script = (
            "#!"
            + sys.executable
            + "\n"
            + """import json, os, sys
from pathlib import Path
if '--version' in sys.argv:
    print('fixture 1.0')
    raise SystemExit(int(os.environ.get('VERSION_EXIT', '0')))
Path(os.environ['CAPTURE']).write_text(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ), 'stdin': sys.stdin.read(), 'cwd': os.getcwd()}))
print('{"type":"result","result":"ok","total_cost_usd":0.01}')
"""
        )
        for name in ("claude", "codex", "opencode", "wrapper cli"):
            binary = self.root / name
            binary.write_text(script)
            binary.chmod(0o755)
        self.patch = patch.dict(
            os.environ,
            {
                "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                "CAPTURE": str(self.capture),
            },
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_argv_environment_and_stdin_on_fake_clis(self):
        base = {**os.environ, **dict.fromkeys(providers.STRIP_ENV, "stale")}
        provider = providers.Provider("test", "anthropic", base_url="https://example.test")
        prompt = self.root / "prompt.md"
        prompt.write_text("task input")
        for sub in (ClaudeSubstrate(), CodexSubstrate()):
            with self.subTest(substrate=sub.name):
                if sub.name == "claude":
                    home = self.root / "claude-home"
                    env = sub.env(base, provider, home)
                    self.assertTrue(home.is_dir())
                    self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(home))
                    self.assertEqual(env["ANTHROPIC_BASE_URL"], provider.base_url)
                    self.assertTrue(
                        all(k not in env for k in providers.STRIP_ENV if k != "ANTHROPIC_BASE_URL")
                    )
                else:
                    home = self.root / "codex-home"
                    codex_provider = providers.Provider(
                        "test", "openai", base_url="https://example.test/v1", api_key_env="TEST_KEY"
                    )
                    sub.write_home(home, codex_provider, "test-model", "high")
                    self.assertEqual(
                        (home / "config.toml").read_text(),
                        providers.codex_config(codex_provider, "test-model", "high"),
                    )
                    env = sub.env(base, home)
                    self.assertEqual(env["CODEX_HOME"], str(home))
                    self.assertTrue(all(k not in env for k in providers.STRIP_ENV))
                    self.assertEqual(sub.exec_env(base, codex_home=home), env)
                    self.assertEqual(sub.cost_usd(prompt), 0.0)
                argv = sub.argv(prompt, self.root, "test-model", "high")
                with prompt.open() as stdin:
                    subprocess.run(
                        argv, cwd=self.root, env=env, stdin=stdin, check=True, capture_output=True
                    )
                capture = json.loads(self.capture.read_text())
                self.assertEqual(capture["stdin"], "task input")
                self.assertEqual(Path(capture["cwd"]), self.root.resolve())
                self.assertEqual(capture["env"]["PATH"], base["PATH"])
                if sub.name == "claude":
                    self.assertEqual(
                        capture["argv"],
                        [
                            "-p",
                            "--output-format",
                            "stream-json",
                            "--verbose",
                            "--dangerously-skip-permissions",
                            "--setting-sources",
                            "project,local",
                            "--model",
                            "test-model",
                            "--effort",
                            "high",
                        ],
                    )
                else:
                    self.assertEqual(
                        capture["argv"],
                        [
                            "exec",
                            "--json",
                            "--skip-git-repo-check",
                            "--dangerously-bypass-approvals-and-sandbox",
                            "--dangerously-bypass-hook-trust",
                            "--ephemeral",
                            "-C",
                            str(self.root),
                            "-",
                        ],
                    )

    def test_wrapped_binary_overrides_legacy_argv_and_preflight(self):
        wrapper = shlex.quote(str(self.root / "wrapper cli")) + " --wrapper"
        for cls in (ClaudeSubstrate, CodexSubstrate, OpencodeSubstrate):
            sub = cls(bin=wrapper)
            sub.preflight()
            with patch.dict(os.environ, {"VERSION_EXIT": "2"}):
                with self.assertRaisesRegex(ConfigError, "version check failed"):
                    sub.preflight()
            with self.assertRaisesRegex(ConfigError, "not found"):
                cls(bin="nonexistent-harness2-binary").preflight()
        cx = CodexSubstrate(bin="codex")
        argv = cx.exec_argv("task", self.root, "model", reasoning_effort="low", bin=wrapper)
        self.assertEqual(argv[:3], [str(self.root / "wrapper cli"), "--wrapper", "exec"])
        self.assertIn('model_reasoning_effort="low"', argv)
        oc = OpencodeSubstrate(bin=wrapper)
        argv = oc.run_argv(
            "task",
            self.root,
            "test/model",
            title="title",
            agent="helper",
            variant="high",
            session="session",
        )
        self.assertEqual(
            argv[:5], [str(self.root / "wrapper cli"), "--wrapper", "run", "task", "--dir"]
        )
        self.assertEqual(
            argv[-6:], ["--agent", "helper", "--variant", "high", "--session", "session"]
        )
        self.assertEqual(
            oc.run_argv("task", self.root, "test/model", bin="opencode")[0],
            str(self.root / "opencode"),
        )


if __name__ == "__main__":
    unittest.main()
