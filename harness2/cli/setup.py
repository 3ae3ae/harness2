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

"""Dispatch setup arguments to an installed suite."""

from __future__ import annotations

import argparse

from .. import registry
from ..settings import ConfigError


def add_parser(subparsers):
    p = subparsers.add_parser(
        "setup",
        help="install what a plugin suite needs",
        description="Run a plugin suite's installer, for example `harness2 setup lab`. "
        "Arguments after the suite name are passed to the installer.",
    )
    p.add_argument("suite", help="suite name")
    p.add_argument("args", nargs=argparse.REMAINDER, help="arguments for the installer")
    return p


def run(args, ctx) -> int:
    ctx.settings
    suite = registry.load(args.suite)
    if not hasattr(suite, "setup"):
        raise ConfigError(f"suite {args.suite!r} has no setup command")
    return suite.setup(args.args)
