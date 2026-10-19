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

"""Discover suite modules without importing them until selected."""

from __future__ import annotations

import importlib
import importlib.metadata
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from .settings import ConfigError, Settings


@dataclass(frozen=True)
class SuiteContext:
    """The project and run settings supplied to a suite's adapter factory."""

    settings: Settings
    project: Path
    run_root: Path
    split_file: Path | None = None


def names() -> list[str]:
    installed = importlib.metadata.entry_points(group="harness2.suites")
    return ["tasks", *sorted({entry.name for entry in installed} - {"tasks"})]


def load(name: str) -> ModuleType:
    if name == "tasks":
        return importlib.import_module("harness2.suite")
    for entry in importlib.metadata.entry_points(group="harness2.suites"):
        if entry.name == name:
            return entry.load()
    raise ConfigError(f"unknown suite {name!r}; available suites: {', '.join(names())}")
