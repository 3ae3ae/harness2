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

"""Offline configuration and endpoint persistence checks for the provider bridge."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2 import providers, settings
from harness2.cli import bridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        (self.project / ".harness2").mkdir(parents=True)
        self.binary = self.root / "bin/litellm"
        self.binary.parent.mkdir()
        self.binary.write_text("fixture")

    def test_endpoint_reuses_key_and_port_and_has_private_permissions(self):
        path = self.root / "state/bridge.json"
        with patch.object(bridge, "_free_port", return_value=12345) as free:
            initial = bridge._endpoint(path, "127.0.0.1", 0)
            self.assertEqual(initial["port"], 12345)
            self.assertEqual(bridge._endpoint(path, "127.0.0.1", 0), initial)
            self.assertEqual(free.call_count, 1)
            updated = bridge._endpoint(path, "127.0.0.1", 23456)
            self.assertEqual(updated, {"port": 23456, "key": initial["key"]})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), updated)
        self.assertGreater(bridge._free_port("127.0.0.1"), 0)

    def test_provider_config_and_state_without_launching_server(self):
        for kind, extra in (
            ("anthropic", 'api_key_env="TEST_KEY"\nbase_url="https://example.test"'),
            ("openai", 'api_key_env="TEST_KEY"\nbase_url="https://example.test/v1"'),
            ("vertex", 'project="test-project"\nregion="test-region"'),
            ("bedrock", 'region="test-region"'),
        ):
            with self.subTest(kind=kind):
                (self.project / ".harness2/config.toml").write_text(
                    f'[providers.custom]\ntype="{kind}"\n{extra}\n'
                )
                stdout = io.StringIO()
                cache = self.root / kind
                with (
                    patch.dict(
                        os.environ,
                        {
                            "XDG_CONFIG_HOME": str(self.root / "config"),
                            "XDG_CACHE_HOME": str(cache),
                            "TEST_KEY": "secret",
                        },
                    ),
                    patch.object(bridge.settings, "find_project", return_value=self.project),
                    patch.object(bridge.sys, "executable", str(self.binary.with_name("python"))),
                    patch.object(bridge.os, "execve") as execute,
                    contextlib.redirect_stdout(stdout),
                ):
                    bridge.main(["--model", "custom/test-model", "--port", "12345"])
                    expected = providers.litellm_params(
                        settings.load(self.project).provider("custom"), "test-model"
                    )
                root = cache / "harness2/bridge"
                config = json.loads((root / "bridge.yaml").read_text())
                self.assertEqual(
                    config["model_list"], [{"model_name": "test-model", "litellm_params": expected}]
                )
                self.assertEqual(
                    config["general_settings"]["master_key"], "os.environ/HARNESS2_BRIDGE_KEY"
                )
                binary, argv, env = execute.call_args.args
                self.assertEqual(binary, str(self.binary))
                self.assertEqual(
                    argv,
                    [
                        str(self.binary),
                        "--config",
                        str(root / "bridge.yaml"),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "12345",
                    ],
                )
                endpoint = json.loads((root / "bridge.json").read_text())
                self.assertEqual(env["HARNESS2_BRIDGE_KEY"], endpoint["key"])
                self.assertIn("[providers.bridge]", stdout.getvalue())
                self.assertIn('api_key_env = "HARNESS2_BRIDGE_KEY"', stdout.getvalue())
                text = stdout.getvalue()
                snippet = text[text.index("[providers.bridge]") : text.index("\n\nand put its key")]
                self.assertEqual(tomllib.loads(snippet)["providers"]["bridge"]["type"], "openai")
                self.assertNotIn("secret", (root / "bridge.yaml").read_text())

    def test_model_required_and_unknown_provider_fails(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                bridge.main([])
            self.assertEqual(raised.exception.code, 2)
        with (
            patch.object(bridge.sys, "executable", str(self.binary.with_name("python"))),
            patch.object(bridge.settings, "find_project", return_value=self.project),
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as raised:
                bridge.main(["--model", "unknown/model"])
            self.assertEqual(raised.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
