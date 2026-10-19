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

"""Offline coverage for the benchmark distribution boundary."""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.metadata import entry_points
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import harness2_benchmarks
from harness2_benchmarks import config, install, jb_opencode_runner, preflight

SUITES = ("jb", "lab", "wb-office", "wb-web", "wb-code")
PLUGIN = Path(__file__).resolve().parents[1]


class PluginTests(unittest.TestCase):
    def context(self, root, executor="opencode"):
        return SimpleNamespace(
            settings={"executor.name": executor},
            project=root,
            run_root=root / "runs" / "one",
            split_file=root / "split.json",
        )

    def suites(self):
        return [
            importlib.import_module("harness2_benchmarks.suites." + name.replace("-", "_"))
            for name in SUITES
        ]

    def test_import_every_module_and_entry_points(self):
        for module in pkgutil.walk_packages(
            harness2_benchmarks.__path__, harness2_benchmarks.__name__ + "."
        ):
            importlib.import_module(module.name)
        entries = {e.name: e for e in entry_points(group="harness2.suites")}
        for name, suite in zip(SUITES, self.suites()):
            self.assertIs(entries[name].load(), suite)
        self.assertEqual(install.missing_runners(), "")

    def test_suites_reject_unsupported_executor(self):
        for name, suite in zip(SUITES, self.suites()):
            for executor in ("claude", "command", "unknown"):
                with self.subTest(suite=name, executor=executor):
                    with self.assertRaisesRegex(
                        config.ConfigError,
                        f"the {name} suite runs on the opencode or codex executor",
                    ):
                        suite.make_adapter(self.context(Path("/project"), executor))

    def test_suite_paths_and_substrates(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, suite in zip(SUITES, self.suites()):
                for executor, substrate in (("opencode", "oc"), ("codex", "cx")):
                    with self.subTest(suite=name, executor=executor):
                        ctx = self.context(Path(tmp), executor)
                        adapter = suite.make_adapter(ctx)
                        self.assertEqual(adapter.spec.name, name)
                        self.assertEqual(adapter.spec.runs_root, ctx.run_root)
                        self.assertEqual(adapter.substrate.name, substrate)
                        self.assertEqual(adapter.split_file, ctx.split_file)
                        if name == "jb":
                            self.assertEqual(adapter.store, ctx.run_root / "dataset")
                        elif name == "lab":
                            self.assertEqual(
                                adapter._results_root, ctx.run_root / "results" / substrate
                            )
                        else:
                            self.assertEqual(
                                adapter._label_dir("example"), ctx.run_root / "rollouts/example"
                            )

    def test_suite_check_and_setup(self):
        for name, suite in zip(SUITES, self.suites()):
            ctx = self.context(Path("/project"))
            with self.subTest(suite=name), patch.object(suite, "make_adapter") as adapter:
                with patch.object(preflight, "check", return_value=["missing"]) as check:
                    self.assertEqual(suite.check(ctx), ["missing"])
                    adapter.assert_called_once_with(ctx)
                    check.assert_called_once_with(adapter.return_value, judge=True)
            bench, _, subset = name.partition("-")
            args = [bench] + (["--subset", subset] if subset else []) + ["--dry-run"]
            with patch.object(suite, "main", return_value=7) as setup:
                self.assertEqual(suite.setup(["--dry-run"]), 7)
                setup.assert_called_once_with(args)

    def test_channels_keep_legacy_validation(self):
        with patch.object(config, "PROPOSER_MODEL_BY_CHANNEL", {"opencode": ""}):
            self.assertEqual(config.resolve_channel_model(None, None).model, "")
        self.assertEqual(
            config.resolve_channel_model("opencode", "provider/model").model, "provider/model"
        )
        self.assertEqual(config.resolve_channel_model("claude_cli", "claude-x").model, "claude-x")
        for kind, model in (
            (None, "x"),
            ("opencode", None),
            ("unknown", "x"),
            ("opencode", ""),
            ("opencode", "bare"),
            ("claude_cli", "provider/model"),
        ):
            with self.subTest(kind=kind, model=model), self.assertRaises(ValueError):
                config.resolve_channel_model(kind, model)

    def test_channels_supply_pinned_opencode_and_vertex_provider(self):
        with (
            patch.object(config, "VERTEX_PROJECT", "test-project"),
            patch.object(config, "VERTEX_REGION", "test-region"),
        ):
            ch = config.resolve_channel_model("opencode", "provider/model", steps=3, timeout=4)
            self.assertEqual(
                ch.bin,
                f"{config.BUN} run --cwd {config.OPENCODE_DIR}/packages/opencode "
                "--conditions=browser src/index.ts",
            )
            self.assertEqual(ch.provider.project, "test-project")
            self.assertEqual(ch.provider.region, "test-region")
            self.assertEqual((ch.steps, ch.timeout), (3, 4))
            self.assertEqual(config.resolve_channel_model("claude_cli", "claude-x").bin, "")
        with patch.object(config, "VERTEX_PROJECT", ""):
            self.assertIsNone(config.resolve_channel_model("opencode", "provider/model").provider)

    def test_dependency_paths_use_cache_or_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "XDG_CACHE_HOME": tmp}
            env.pop("HARNESS2_BENCH_DEPS", None)
            code = "from harness2_benchmarks.config import DEPENDENCIES; print(DEPENDENCIES)"
            for override, expected in (
                (None, Path(tmp) / "harness2/benchmarks"),
                (str(Path(tmp) / "custom"), Path(tmp) / "custom"),
            ):
                if override:
                    env["HARNESS2_BENCH_DEPS"] = override
                result = subprocess.run(
                    [sys.executable, "-B", "-c", code],
                    env=env,
                    cwd=tmp,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(result.stdout.strip(), str(expected.resolve()))
        self.assertEqual(
            config.THIRD_PARTY, Path(harness2_benchmarks.__file__).parent / "third_party"
        )
        self.assertFalse(hasattr(config, "RUNS"))

    def test_preflight_checks_pinned_assets_without_core_opencode_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = Path(tmp)
            oc = deps / "opencode"
            bun = deps / "bun"
            (oc / "node_modules").mkdir(parents=True)
            index = oc / "packages/opencode/src/index.ts"
            index.parent.mkdir(parents=True)
            index.touch()
            bun.touch()
            adapter = SimpleNamespace(
                spec=SimpleNamespace(name="jb"), tasks=Mock(), preflight=Mock()
            )
            with (
                patch.multiple(
                    config,
                    DEPENDENCIES=deps,
                    OPENCODE_DIR=oc,
                    BUN=bun,
                    SOLVER_MODEL_OC="provider/model",
                    IMPROVER_MODEL="provider/model",
                    validate_models=Mock(),
                ),
                patch(
                    "harness2_benchmarks.preflight.subprocess.run",
                    return_value=SimpleNamespace(returncode=0),
                ),
                patch(
                    "harness2.substrate.opencode.OpencodeSubstrate.preflight",
                    side_effect=AssertionError,
                ),
            ):
                self.assertEqual(preflight.check(adapter), [])
                index.unlink()
                bun.unlink()
                errors = preflight.check(adapter)
                self.assertTrue(any("OpenCode checkout missing" in e for e in errors))
                self.assertTrue(any("bun not found" in e for e in errors))

    def test_codex_preflight_uses_plugin_paths_and_validates_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "codex"
            binary.touch()
            with (
                patch.multiple(
                    config,
                    CODEX_DIST=tmp,
                    CODEX_BASE_URL="http://localhost:4000/v1",
                    CODEX_CONTAINER_URL="http://host.docker.internal:4000/v1",
                ),
                patch.dict(
                    os.environ,
                    HARNESS2_CODEX_BASE_URL="http://localhost:4000/v1",
                    HARNESS2_CODEX_API_KEY="test-key",
                ),
                patch("harness2_benchmarks.preflight.socket.create_connection") as connect,
            ):
                preflight.codex(require_bridge=True)
                connect.assert_called_once_with(("localhost", 4000), timeout=5)
                with patch.object(config, "CODEX_BASE_URL", "file:///tmp/key"):
                    with self.assertRaises(config.ConfigError):
                        preflight.codex()
                with patch.object(config, "CODEX_CONTAINER_URL", ""):
                    with self.assertRaisesRegex(RuntimeError, "HARNESS2_CODEX_CONTAINER_URL"):
                        preflight.codex(require_bridge=True)
                binary.unlink()
                with self.assertRaisesRegex(RuntimeError, "Codex is not installed"):
                    preflight.codex()

    def test_jobbench_runner_defaults_use_plugin_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            render = root / "render"
            (render / ".opencode").mkdir(parents=True)
            oc = root / "opencode"
            (oc / "packages/opencode").mkdir(parents=True)
            with (
                patch.dict(
                    os.environ,
                    {
                        "MODEL_ID": "provider/model",
                        "LABEL": "test",
                        "TASK_IDS": "task",
                        "HARNESS_RENDER_DIR": str(render),
                        "OPENCODE_DIR": "",
                        "TASKS_BASE_DIR": "",
                    },
                ),
                patch.object(config, "OPENCODE_DIR", oc),
                patch.object(jb_opencode_runner, "JOBBENCH", root / "jb"),
            ):
                settings = jb_opencode_runner._settings()
                self.assertEqual(settings.opencode_dir, oc)
                self.assertEqual(settings.tasks_base, root / "jb/dataset/main")

    def test_lab_overlay_paths_resolve_in_downloaded_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patch.object(config, "DEPENDENCIES", root):
                install.overlays("harvey-labs")
            repo = root / "harvey-labs"
            parsers = repo / "sandbox/parsers"
            parsers.mkdir(parents=True)
            (parsers / "parse_doc.py").touch()
            code = (
                "import json, runpy, sys; from pathlib import Path; repo = Path(sys.argv[1]); "
                "r = runpy.run_path(str(repo / 'external/adapt_runs_cx/run_codex_lab.py')); "
                "p = runpy.run_path(str(repo / 'opencode/scripts/read_doc.py')); "
                "print(json.dumps([str(r['REPO']), str(r['C'].BENCH_ROOT), "
                "str(r['READ_DOC']), str(p['_find_parsers_dir']())]))"
            )
            env = dict(os.environ)
            env.pop("LAB_BENCH_ROOT", None)
            result = subprocess.run(
                [sys.executable, "-B", "-c", code, str(repo)],
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
            self.assertEqual(
                json.loads(result.stdout),
                [str(repo), str(repo), str(repo / "opencode/scripts/read_doc.py"), str(parsers)],
            )

    def test_codex_shell_runner_defaults_use_benchmark_cache(self):
        runner = config.THIRD_PARTY / "jb_runner/run_benchmark_codex_cli_adapt.sh"
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "XDG_CACHE_HOME": tmp, "BENCHMARK_MODELS": "test"}
            env.pop("TASKS_BASE_DIR", None)
            env.pop("HARNESS2_BENCH_DEPS", None)
            env.pop("SPLIT", None)
            for override, expected in (
                (None, Path(tmp) / "harness2/benchmarks/job-bench-eval/dataset/main"),
                (str(Path(tmp) / "custom"), Path(tmp) / "custom/job-bench-eval/dataset/main"),
            ):
                if override:
                    env["HARNESS2_BENCH_DEPS"] = override
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'source "$1"; printf "%s" "$TASKS_BASE_DIR"',
                        "bash",
                        str(runner),
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(result.stdout, str(expected))

    def test_scripts_use_new_cli_and_keep_project_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "plugins/harness2-benchmarks/scripts"
            scripts.mkdir(parents=True)
            binary = root / ".venv/bin/harness2"
            binary.parent.mkdir(parents=True)
            binary.write_text(
                f"#!{sys.executable}\nimport json, os, sys\n"
                "with open(os.environ['CALLS'], 'a') as f:\n"
                "    f.write(json.dumps([os.getcwd(), sys.argv[1:]]) + '\\n')\n"
            )
            binary.chmod(0o755)
            for filename, suite in (
                ("run_jobbench.sh", "jb"),
                ("run_lab.sh", "lab"),
                ("run_workbuddy.sh", "wb-web"),
            ):
                script = scripts / filename
                shutil.copy2(PLUGIN / "scripts" / filename, script)
                for substrate, executor in (("oc", "opencode"), ("cx", "codex")):
                    for dry in (False, True):
                        calls = root / "calls.jsonl"
                        calls.unlink(missing_ok=True)
                        env = {
                            **os.environ,
                            "CALLS": str(calls),
                            "SUBSTRATE": substrate,
                            "SUBSET": "web",
                            "K": "3",
                            "MODE": "sequential",
                        }
                        result = subprocess.run(
                            ["bash", str(script), *(["--dry-run"] if dry else [])],
                            cwd=root,
                            env=env,
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        rows = [json.loads(line) for line in calls.read_text().splitlines()]
                        self.assertTrue(all(row[0] == str(root.resolve()) for row in rows))
                        self.assertEqual(
                            rows[0][1],
                            [
                                "-c",
                                f"executor.name={executor}",
                                "-c",
                                f"suite.name={suite}",
                                "doctor",
                            ],
                        )
                        self.assertEqual(
                            rows[1][1],
                            [
                                "-c",
                                f"executor.name={executor}",
                                "run",
                                "--suite",
                                suite,
                                "--name",
                                f"{suite}-{executor}-k3-sequential",
                                "--k",
                                "3",
                                "--mode",
                                "sequential",
                            ]
                            + (["--dry-run"] if dry else []),
                        )
                        self.assertEqual(len(rows), 2 if dry else 4)
                        if not dry:
                            self.assertEqual(
                                [r[1] for r in rows[2:]],
                                [
                                    ["judge", f"{suite}-{executor}-k3-sequential"],
                                    ["report", f"{suite}-{executor}-k3-sequential"],
                                ],
                            )
                result = subprocess.run(
                    ["bash", str(script)], env={**os.environ, "SUBSTRATE": "bad"}
                )
                self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
