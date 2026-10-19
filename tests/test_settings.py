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

"""Offline tests for layered settings and edits to project TOML files."""

from __future__ import annotations

import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2 import providers, settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        (self.project / ".harness2").mkdir(parents=True)
        self.global_file = self.root / "config/harness2/config.toml"
        self.project_file = settings.project_path(self.project)
        environment = patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}, clear=True
        )
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_defaults_access_and_copy_isolation(self):
        resolved = settings.load(self.project)
        for section, values in settings.DEFAULTS.items():
            self.assertEqual(resolved.section(section), values)
        self.assertEqual(resolved.project, self.project)
        self.assertEqual(resolved.origin("run.k"), "default")
        self.assertIsNone(resolved.get("run.seed"))
        self.assertEqual(resolved.get("run.absent", 4), 4)
        for key in ("absent", "run.absent", "run.k.absent"):
            with self.subTest(key=key), self.assertRaises(KeyError):
                resolved.get(key)
            with self.assertRaises(KeyError):
                resolved.origin(key)
        self.assertEqual(resolved.flat()["executor.editable"], [])
        self.assertNotIn("selector", resolved.flat())
        resolved.section("executor")["editable"].append("one.py")
        resolved.flat()["executor.editable"].append("two.py")
        self.assertEqual(resolved.get("executor.editable"), [])
        self.assertEqual(settings.load(None).section("executor"), settings.DEFAULTS["executor"])

    def test_state_dir_uses_xdg_or_home_without_creating_it(self):
        with patch.dict(os.environ, {"HOME": str(self.root)}, clear=True):
            self.assertEqual(settings.state_dir(), self.root / ".local/state/harness2")
            self.assertFalse(settings.state_dir().exists())
            os.environ["XDG_STATE_HOME"] = str(self.root / "state")
            self.assertEqual(settings.state_dir(), self.root / "state/harness2")
            self.assertFalse(settings.state_dir().exists())
            os.environ["XDG_STATE_HOME"] = ""
            self.assertEqual(settings.state_dir(), self.root / ".local/state/harness2")

    def test_precedence_and_origins_across_all_six_layers(self):
        self.write(self.global_file, "[run]\nk = 2\nworkers = 2\n")
        self.write(
            self.project_file,
            "[run]\nk = 3\nphase_width = 3\n[profiles.fast.run]\nk = 4\n",
        )
        layers = [
            (settings.load(None), 2, str(self.global_file)),
            (settings.load(self.project), 3, str(self.project_file)),
            (settings.load(self.project, profile="fast"), 4, "profile fast"),
        ]
        os.environ["HARNESS2_RUN__K"] = "5"
        layers.append((settings.load(self.project, profile="fast"), 5, "env HARNESS2_RUN__K"))
        layers.append(
            (
                settings.load(
                    self.project,
                    profile="fast",
                    overrides={"run.k": (6, "flag --k")},
                ),
                6,
                "flag --k",
            )
        )
        for resolved, value, origin in layers:
            with self.subTest(origin=origin):
                self.assertEqual(resolved.get("run.k"), value)
                self.assertEqual(resolved.origin("run.k"), origin)
        resolved = layers[-1][0]
        self.assertEqual(resolved.origin("run.workers"), str(self.global_file))
        self.assertEqual(resolved.origin("run.phase_width"), str(self.project_file))
        self.assertEqual(resolved.origin("run.obs_trunc"), "default")

    def test_profiles_merge_files_and_explicit_profile_wins(self):
        self.write(
            self.global_file,
            '[profiles.fast.improver]\nmodel = "anthropic/global"\neffort = "low"\n',
        )
        self.write(
            self.project_file,
            '[profiles.fast.improver]\neffort = "medium"\n[profiles.slow.run]\nk = 7\n',
        )
        os.environ["HARNESS2_PROFILE"] = "fast"
        resolved = settings.load(self.project)
        self.assertEqual(resolved.role("improver")["model"], "anthropic/global")
        self.assertEqual(resolved.role("improver")["effort"], "medium")
        self.assertEqual(resolved.origin("improver.model"), "profile fast")
        self.assertEqual(settings.load(self.project, profile="slow").get("run.k"), 7)
        with self.assertRaisesRegex(settings.ConfigError, "unknown profile 'missing'"):
            settings.load(self.project, profile="missing")
        self.write(self.project_file, '[profiles]\nbad = "invalid"\n')
        with self.assertRaisesRegex(settings.ConfigError, "profiles.bad must be a table"):
            settings.load(self.project, profile="bad")

    def test_env_values_and_dotted_overrides(self):
        os.environ.update(
            {
                "HARNESS2_EXECUTOR__MODEL": "anthropic/model/submodel",
                "HARNESS2_EXECUTOR__EDITABLE": '["a.py", "b.py"]',
                "HARNESS2_PROVIDERS__LOCAL__TYPE": "openai",
                "HARNESS2_PROVIDERS__LOCAL__BASE_URL": "http://localhost:8080/v1",
                "HARNESS2_RUN__K": "3",
                "HARNESS2_RUN__PLUGIN_BOOL": "true",
                "HARNESS2_OLD_OPTION": "ignored",
            }
        )
        resolved = settings.load(self.project, overrides={"run.plugin.nested": ({"x": 1}, "-c")})
        self.assertEqual(resolved.get("executor.model"), "anthropic/model/submodel")
        self.assertEqual(resolved.get("executor.editable"), ["a.py", "b.py"])
        self.assertEqual(resolved.get("run.k"), 3)
        self.assertIs(resolved.get("run.plugin_bool"), True)
        self.assertEqual(resolved.get("run.plugin.nested.x"), 1)
        self.assertEqual(resolved.origin("run.plugin.nested.x"), "-c")
        self.assertEqual(resolved.provider("local").base_url, "http://localhost:8080/v1")
        self.assertNotIn("old_option", resolved.flat())

    def test_parse_value(self):
        for raw, value in (
            ("8", 8),
            ("0.25", 0.25),
            ("false", False),
            ('"hello"', "hello"),
            ('["one", 2, true]', ["one", 2, True]),
            ("{x = 1}", {"x": 1}),
            ("provider/model", "provider/model"),
            ("", ""),
            ("[broken", "[broken"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(settings.parse_value(raw), value)

    def test_role_inheritance_and_human_selector(self):
        resolved = settings.load(
            self.project,
            overrides={
                "improver.model": ("anthropic/root", "-c"),
                "improver.steer": ("keep it small", "-c"),
                "selector.effort": ("low", "-c"),
                "selector.model": ("anthropic/selector", "-c"),
                "reflector.steps": (4, "-c"),
                "judge.timeout": (20, "-c"),
                "judge.plugin_key": ("ignored by role", "-c"),
            },
        )
        root = resolved.role("improver")
        self.assertEqual(root["steer"], "keep it small")
        self.assertEqual(
            resolved.role("selector"),
            {
                key: value
                for key, value in {**root, "effort": "low", "model": "anthropic/selector"}.items()
                if key != "steer"
            },
        )
        self.assertEqual(resolved.role("reflector"), {**resolved.role("selector"), "steps": 4})
        self.assertEqual(
            resolved.role("judge"),
            {key: value for key, value in {**root, "timeout": 20}.items() if key != "steer"},
        )
        human = settings.load(
            self.project,
            overrides={
                "selector.channel": ("human", "-c"),
                "selector.model": ("unused", "-c"),
                "reflector.timeout": (10, "-c"),
            },
        )
        self.assertEqual(human.role("selector")["channel"], "human")
        self.assertEqual(human.role("reflector")["channel"], "claude")
        self.assertEqual(human.role("reflector")["model"], "")
        self.assertEqual(human.role("reflector")["timeout"], 10)
        with self.assertRaisesRegex(settings.ConfigError, "unknown role"):
            resolved.role("executor")

    def test_provider_lookup_and_unknown_plugin_keys(self):
        resolved = settings.load(
            None,
            overrides={
                "providers.local": (
                    {
                        "type": "vertex",
                        "project": "my-project",
                        "region": "us-east1",
                        "plugin": 9,
                    },
                    "-c",
                ),
            },
        )
        self.assertEqual(
            resolved.provider("local"),
            providers.Provider(
                "local",
                "vertex",
                project="my-project",
                region="us-east1",
            ),
        )
        with self.assertRaisesRegex(settings.ConfigError, "unknown provider 'absent'"):
            resolved.provider("absent")

    def test_validation_aggregates_problems_and_rejects_secrets_anywhere(self):
        with self.assertRaises(settings.ConfigError) as caught:
            settings.load(
                self.project,
                overrides={
                    "unexpected.key": (1, "-c"),
                    "executor.api_key": ("never-print-this", "-c"),
                    "run.k": (0, "-c"),
                    "run.workers": (True, "-c"),
                    "run.phase_width": (-1, "-c"),
                    "run.proposer_concurrency": (1.5, "-c"),
                    "run.transport_attempts": ("two", "-c"),
                    "run.budget_usd": (-0.1, "-c"),
                    "run.max_calls": (-1, "-c"),
                    "providers.local.type": ("invalid", "-c"),
                },
            )
        message = str(caught.exception)
        self.assertEqual(len(message.splitlines()), 10)
        self.assertIn(
            "store secrets in environment variables and reference them with api_key_env", message
        )
        self.assertNotIn("never-print-this", message)
        self.assertIn("unknown section 'unexpected'", message)
        for name in ("k", "workers", "phase_width", "proposer_concurrency", "transport_attempts"):
            self.assertIn(f"run.{name} must be a positive integer", message)
        self.assertIn("run.budget_usd must be a non-negative number", message)
        self.assertIn("run.max_calls must be a non-negative number", message)
        self.assertIn("providers.local.type must be one of", message)
        for key, value in (
            ("profiles.unused.executor.api_key", "hidden"),
            ("executor.plugin", [{"api_key": "hidden"}]),
        ):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(settings.ConfigError, "store secrets"),
            ):
                settings.load(None, overrides={key: (value, "-c")})

    def test_malformed_sections_and_non_numeric_limits(self):
        for key, value, message in (
            ("run", "invalid", "run must be a table"),
            ("profiles", 3, "profiles must be a table"),
            ("providers.local", 3, "providers.local must be a table"),
            ("run.max_calls", "many", "run.max_calls must be a non-negative number"),
            ("run.budget_usd", float("nan"), "run.budget_usd must be a non-negative number"),
            ("run.budget_usd", False, "run.budget_usd must be a non-negative number"),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(settings.ConfigError, message):
                settings.load(None, overrides={key: (value, "-c")})
        with self.assertRaisesRegex(settings.ConfigError, "invalid setting key"):
            settings.load(None, overrides={"run..k": (1, "-c")})

    def test_invalid_toml_and_read_error(self):
        self.write(self.project_file, "[broken")
        with self.assertRaisesRegex(settings.ConfigError, "invalid TOML in"):
            settings.load(self.project)
        self.project_file.unlink()
        self.project_file.mkdir()
        with self.assertRaisesRegex(settings.ConfigError, "cannot read"):
            settings.load(self.project)

    def test_locations_find_project_and_path_resolution(self):
        self.assertEqual(settings.global_path(), self.global_file)
        os.environ["XDG_CACHE_HOME"] = str(self.root / "cache")
        self.assertEqual(settings.cache_dir(), self.root / "cache/harness2")
        os.environ.pop("XDG_CACHE_HOME")
        os.environ.pop("XDG_CONFIG_HOME")
        with patch.object(Path, "home", return_value=self.root):
            self.assertEqual(settings.global_path(), self.root / ".config/harness2/config.toml")
            self.assertEqual(settings.cache_dir(), self.root / ".cache/harness2")
            os.environ["XDG_CONFIG_HOME"] = ""
            self.assertEqual(settings.global_path(), self.root / ".config/harness2/config.toml")
        child = self.project / "src/deep"
        child.mkdir(parents=True)
        self.assertEqual(settings.find_project(child), self.project)
        self.assertEqual(settings.find_project(self.project), self.project)
        (child / ".harness2").mkdir()
        self.assertEqual(settings.find_project(child), child)
        (self.root / ".harness2").touch()
        self.assertIsNone(settings.find_project(self.root))
        resolved = settings.load(self.project)
        self.assertEqual(resolved.path("paths.tasks"), self.project / ".harness2/tasks")
        absolute = settings.load(
            self.project, overrides={"paths.runs": (str(self.root / "runs"), "-c")}
        )
        self.assertEqual(absolute.path("paths.runs"), self.root / "runs")
        self.assertEqual(settings.load(None).path("paths.harness"), Path.cwd().resolve())

    def test_dotenv_parser_and_load_order(self):
        dotenv = self.project / ".harness2/.env"
        self.write(
            dotenv,
            (
                "# comment\n\nexport SHELL_VALUE = ignored\n"
                "DOUBLE = \"two # literal\"\nSINGLE = 'one'\nPLAIN = value # note\n"
                "EMPTY=\nINVALID-KEY=no\nno assignment\n = invalid\n"
                "HARNESS2_RUN__K=4\nHARNESS2_PROFILE=fromenv\n"
            ),
        )
        self.write(self.project_file, "[profiles.fromenv.run]\nworkers = 2\n")
        os.environ["SHELL_VALUE"] = "shell"
        resolved = settings.load(self.project)
        self.assertEqual(resolved.get("run.k"), 4)
        self.assertEqual(resolved.get("run.workers"), 2)
        self.assertEqual(resolved.origin("run.k"), "env HARNESS2_RUN__K")
        self.assertEqual(os.environ["SHELL_VALUE"], "shell")
        self.assertEqual(os.environ["DOUBLE"], "two # literal")
        self.assertEqual(os.environ["SINGLE"], "one")
        self.assertEqual(os.environ["PLAIN"], "value")
        self.assertEqual(os.environ["EMPTY"], "")
        self.assertNotIn("INVALID-KEY", os.environ)
        settings.load_dotenv(self.root / "missing")
        settings.load_dotenv(self.root)

    def test_set_value_preserves_comments_and_other_sections(self):
        original = '# heading\n[run] # tuning\nk = 1 # count\n\n[executor]\nmodel = "original"\n'
        self.write(self.project_file, original)
        settings.set_value(self.project_file, "run.k", 3)
        self.assertEqual(self.project_file.read_text(), original.replace("k = 1", "k = 3"))
        settings.set_value(self.project_file, "run.workers", 2)
        settings.set_value(self.project_file, "providers.local.type", "openai")
        parsed = tomllib.loads(self.project_file.read_text())
        self.assertEqual(parsed["run"], {"k": 3, "workers": 2})
        self.assertEqual(parsed["providers"]["local"]["type"], "openai")
        self.assertEqual(parsed["executor"], {"model": "original"})
        self.assertIn("# heading\n[run] # tuning\nk = 3 # count\n", self.project_file.read_text())

    def test_set_value_round_trips_literals_and_creates_file(self):
        for name, value in {
            "string": 'quote " and \\ and\nnewline 😀\x7f',
            "bool": True,
            "int": 2,
            "float": 0.25,
            "array": ["one # literal", False, 2, 0.5],
            "empty": [],
        }.items():
            with self.subTest(name=name):
                path = self.root / "new/nested/config.toml"
                settings.set_value(path, "custom." + name, value)
                parsed = tomllib.loads(path.read_text())
                self.assertEqual(parsed["custom"][name], value)
        settings.set_value(path, "custom.a spaced key", "quoted key")
        self.assertEqual(tomllib.loads(path.read_text())["custom"]["a spaced key"], "quoted key")

    def test_set_value_multiline_values_root_keys_and_line_endings(self):
        original = (
            '# top\r\nroot = 1\r\n["executor"]\r\neditable = [\r\n'
            '    "one#file", # first\r\n    "two", # second\r\n] # files\r\n'
            'command = """first\r\n# inside string\r\nlast""" # command\r\n'
            "[other]\r\nkeep = 'same'\r\n"
        )
        self.project_file.write_bytes(original.encode())
        settings.set_value(self.project_file, "executor.editable", ["new"])
        settings.set_value(self.project_file, "executor.command", 'quoted " # literal')
        settings.set_value(self.project_file, "added", False)
        result = self.project_file.read_bytes().decode()
        self.assertNotIn("one#file", result)
        for comment in ("# top", "# first", "# second", "# files", "# command"):
            self.assertIn(comment, result)
        self.assertNotIn("# inside string", result)
        self.assertIn("[other]\r\nkeep = 'same'\r\n", result)
        self.assertNotIn("\n", result.replace("\r\n", ""))
        parsed = tomllib.loads(result)
        self.assertIs(parsed["added"], False)
        self.assertEqual(parsed["executor"]["editable"], ["new"])
        self.assertEqual(parsed["executor"]["command"], 'quoted " # literal')

    def test_set_value_no_final_newline_and_invalid_input_do_not_destroy_file(self):
        self.write(self.project_file, "[run]\nk = 1")
        settings.set_value(self.project_file, "run.workers", 2)
        self.assertEqual(tomllib.loads(self.project_file.read_text())["run"]["workers"], 2)
        before = self.project_file.read_text()
        for value in (None, {}, [[1]], [None], "\ud800"):
            with self.subTest(value=value), self.assertRaises(settings.ConfigError):
                settings.set_value(self.project_file, "run.value", value)
            self.assertEqual(self.project_file.read_text(), before)
        with self.assertRaisesRegex(settings.ConfigError, "invalid setting key"):
            settings.set_value(self.project_file, "run..k", 2)
        self.write(self.project_file, "[invalid")
        with self.assertRaisesRegex(settings.ConfigError, "invalid TOML"):
            settings.set_value(self.project_file, "run.k", 2)
        self.assertEqual(self.project_file.read_text(), "[invalid")

    def test_set_value_multiline_quotes_and_escaped_hashes(self):
        for literal in (
            "'literal # hash'",
            '"escaped \\" # hash"',
            "'''multiline # hash\nends with quote''''",
            '"""multiline # hash\nends with quote""""',
        ):
            with self.subTest(literal=literal):
                self.write(self.project_file, f"[suite]\nwork = {literal} # retain\n")
                settings.set_value(self.project_file, "suite.work", "new")
                self.assertEqual(self.project_file.read_text(), '[suite]\nwork = "new" # retain\n')


if __name__ == "__main__":
    unittest.main()
