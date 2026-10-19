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
    p = subparsers.add_parser(
        "doctor",
        help="check that a run has what it needs",
        description="Check the agent CLIs, provider keys, settings and tasks without calling a "
        "model. --live also sends one tiny request per configured model.",
    )
    p.add_argument("--live", action="store_true", help="also call each model once (costs a little)")
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

    check(f"suite {config.get('suite.name')}", suite_check)
    names = ("executor", "improver", "selector", "reflector", "judge")
    roles = [config.role(name) for name in names[1:]]
    roles.insert(0, {**config.section("executor"), "channel": config.get("executor.name")})
    binaries, models, referenced = set(), {}, set()
    for role_name, role in zip(names, roles):
        channel = role["channel"]
        if channel == "human":
            continue
        if channel != "command":
            binaries.add((role["bin"], channel))
        model = role["model"]
        if model:

            def resolve(model=model, channel=channel, role=role):
                provider_name, bare = providers.split_model(model)
                provider = config.provider(provider_name)
                referenced.add(provider_name)
                # each raises ConfigError when this CLI cannot use this kind of provider
                if channel == "claude":
                    providers.claude_env(provider)
                elif channel == "codex":
                    providers.codex_args(provider, bare, role["effort"])
                elif channel == "opencode":
                    providers.opencode_config(provider, bare)
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

                return f"{channel} · {model}"

            check(role_name, resolve)
        elif channel != "command":
            rows.append(
                {
                    "check": role_name,
                    "ok": False,
                    "detail": f"set {role_name}.model (provider/model)",
                }
            )
    for binary, channel in sorted(binaries):

        def binary_check(binary=binary, channel=channel):
            argv = cli_argv(binary, channel)
            result = subprocess.run(
                [*argv, "--version"], check=True, capture_output=True, text=True, timeout=10
            )
            return (result.stdout or result.stderr).strip()

        check(f"{binary or channel} CLI", binary_check)
    for name in sorted(referenced):
        provider = config.provider(name)
        problems = providers.check(provider)
        detail = f"key in {provider.api_key_env}" if provider.api_key_env else "ok"
        rows.append(
            {
                "check": f"provider {name}",
                "ok": not problems,
                "detail": "; ".join(problems) or detail,
            }
        )
        if provider.type == "chatgpt-login":
            home = providers.codex_login_home(provider)
            files = [
                str(path)
                for name in ("AGENTS.md", "hooks.json")
                if (path := home / name).is_file() and path.stat().st_size
            ]
            if files:
                rows.append(
                    {
                        "check": "Codex home",
                        "ok": True,
                        "detail": " and ".join(files) + " are part of every Codex run",
                    }
                )
    if args.live:
        for (channel, model), spec in sorted(models.items()):

            def live_check(spec=spec):
                with ctx.progress():
                    reply = Proposer(spec).call('Reply "ok".', None, "doctor.live")
                if not reply.ok or reply.text.strip().lower() != "ok":
                    raise RuntimeError(reply.error or "expected reply ok")
                return reply.text

            check(f"live {channel} · {model}", live_check)
    ctx.out(rows, _render)
    return int(any(not row["ok"] for row in rows))


def _render(rows: list[dict]) -> str:
    width = max(len(row["check"]) for row in rows)
    lines = [
        f"{'ok' if row['ok'] else 'FAIL':<4}  {row['check']:<{width}}  {row['detail']}"
        for row in rows
    ]
    failed = sum(not row["ok"] for row in rows)
    lines.append("")
    lines.append("All checks passed." if not failed else f"{failed} problem(s) to fix.")
    return "\n".join(lines)
