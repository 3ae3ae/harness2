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

"""Check suite configuration and model clients."""

from __future__ import annotations

import subprocess

from .. import providers, registry
from ..core.proposer import Proposer
from ..core.spec import ChannelSpec
from ..substrate._native import cli_argv
from . import _common as C


def add_parser(subparsers):
    p = subparsers.add_parser("doctor")
    p.add_argument("--live", action="store_true", help="Call each model; costs money.")
    return p


def run(args, ctx) -> int:
    config = ctx.settings
    rows = []

    def check(name, operation):
        try:
            detail = operation()
            rows.append({"check": name, "ok": True, "detail": detail or "ok"})
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            rows.append({"check": name, "ok": False, "detail": str(exc)})

    def suite_check():
        suite = registry.load(config.get("suite.name"))
        suite_ctx = registry.SuiteContext(
            config, ctx.project, C.run_path(ctx, C.default_name(config))
        )
        with ctx.progress():
            problems = suite.check(suite_ctx) if hasattr(suite, "check") else []
        if problems:
            raise RuntimeError("; ".join(problems))

    check("suite", suite_check)
    roles = [config.role(name) for name in ("improver", "selector", "reflector", "judge")]
    roles.insert(0, {**config.section("executor"), "channel": config.get("executor.name")})
    binaries, models, referenced = set(), {}, set()
    for role in roles:
        channel = role["channel"]
        if channel == "human":
            continue
        if channel != "command":
            binaries.add((role["bin"], channel))
        model = role["model"]
        if model:

            def resolve(model=model, channel=channel, role=role):
                provider_name, _ = providers.split_model(model)
                provider = config.provider(provider_name)
                referenced.add(provider_name)
                if channel != "command":
                    models.setdefault(
                        (channel, model),
                        ChannelSpec(
                            channel,
                            model,
                            role["effort"],
                            role.get("steps", 120),
                            role["timeout"],
                            role["bin"],
                            provider,
                        ),
                    )

            check(f"model {channel}/{model}", resolve)
        elif channel != "command":
            rows.append({"check": f"model {channel}", "ok": False, "detail": "model is empty"})
    for binary, channel in sorted(binaries):

        def binary_check(binary=binary, channel=channel):
            argv = cli_argv(binary, channel)
            result = subprocess.run(
                [*argv, "--version"], check=True, capture_output=True, text=True, timeout=10
            )
            return (result.stdout or result.stderr).strip()

        check(f"binary {binary or channel}", binary_check)
    for name in sorted(referenced):
        problems = providers.check(config.provider(name))
        rows.append(
            {"check": f"provider {name}", "ok": not problems, "detail": "; ".join(problems) or "ok"}
        )
    if args.live:
        for (channel, model), spec in sorted(models.items()):

            def live_check(spec=spec):
                with ctx.progress():
                    reply = Proposer(spec).call('Reply "ok".', None, "doctor.live")
                if not reply.ok or reply.text.strip().lower() != "ok":
                    raise RuntimeError(reply.error or "expected reply ok")
                return reply.text

            check(f"live {channel}/{model}", live_check)
    ctx.out(
        rows,
        lambda data: "\n".join(
            f"{'ok' if row['ok'] else 'FAIL'} {row['check']}: {row['detail']}" for row in data
        ),
    )
    return int(any(not row["ok"] for row in rows))
