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

"""wb-office suite entry point."""

from __future__ import annotations

from .. import preflight
from ..config import ConfigError
from ..setup import main
from ..workbuddy import WbAdapter


def make_adapter(ctx):
    try:
        substrate = {"opencode": "oc", "codex": "cx"}[ctx.settings.get("executor.name")]
    except KeyError as exc:
        raise ConfigError("the wb-office suite runs on the opencode or codex executor") from exc
    return WbAdapter(
        "office", substrate=substrate, runs_root=ctx.run_root, split_file=ctx.split_file
    )


def check(ctx) -> list[str]:
    return preflight.check(make_adapter(ctx), judge=True)


def setup(argv: list[str]) -> int:
    return main(["wb", "--subset", "office", *argv])
