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

"""Suite discovery with isolated, fake entry points."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from importlib.metadata import EntryPoint
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from harness2 import registry
from harness2.settings import ConfigError


class RegistryTests(unittest.TestCase):
    def test_builtin_is_listed_first_and_imported_lazily(self):
        entries = [
            EntryPoint("zeta", "fake.zeta", "harness2.suites"),
            EntryPoint("alpha", "fake.alpha", "harness2.suites"),
            EntryPoint("tasks", "fake.tasks", "harness2.suites"),
        ]
        module = ModuleType("harness2.suite")
        with (
            patch("importlib.metadata.entry_points", return_value=entries) as discover,
            patch("importlib.import_module", return_value=module) as import_module,
        ):
            self.assertEqual(registry.names(), ["tasks", "alpha", "zeta"])
            import_module.assert_not_called()
            self.assertIs(registry.load("tasks"), module)
            import_module.assert_called_once_with("harness2.suite")
            discover.assert_called_once_with(group="harness2.suites")

    def test_plugin_module_is_loaded_through_entry_point(self):
        module = ModuleType("fake_suite")
        module.make_adapter = lambda ctx: ctx.project
        entry = EntryPoint("custom", "fake_suite", "harness2.suites")
        with (
            patch("importlib.metadata.entry_points", return_value=[entry]) as discover,
            patch.dict("sys.modules", {"fake_suite": module}),
        ):
            self.assertEqual(registry.names(), ["tasks", "custom"])
            self.assertIs(registry.load("custom"), module)
            discover.assert_called_with(group="harness2.suites")
            context = registry.SuiteContext(None, Path("/project"), Path("/project/runs/name"))
            self.assertEqual(module.make_adapter(context), Path("/project"))

    def test_unknown_suite_lists_available_names(self):
        entry = EntryPoint("example", "fake.example", "harness2.suites")
        with patch("importlib.metadata.entry_points", return_value=[entry]):
            with self.assertRaisesRegex(
                ConfigError, "unknown suite 'missing'; available suites: tasks, example"
            ):
                registry.load("missing")
        with patch("importlib.metadata.entry_points", return_value=[]):
            self.assertEqual(registry.names(), ["tasks"])

    def test_suite_context_is_frozen_with_optional_split_file(self):
        context = registry.SuiteContext(None, Path("/project"), Path("/runs"))
        self.assertIsNone(context.split_file)
        with self.assertRaises(FrozenInstanceError):
            context.project = Path("/changed")
        split = Path("/split.json")
        self.assertEqual(
            registry.SuiteContext(None, Path("/p"), Path("/r"), split).split_file, split
        )


if __name__ == "__main__":
    unittest.main()
