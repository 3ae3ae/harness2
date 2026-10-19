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

"""Show whether the subscription logins harness2 uses are ready, and set up Codex's.

claude-login uses Claude Code's own login as it is: runs switch off your personal settings,
hooks and plugins with flags. Codex has no such switch for its hooks.json and AGENTS.md, so
chatgpt-login runs use a Codex home of their own, signed in once with `harness2 login codex`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

from .. import providers


def add_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "login",
        help="check the subscription logins, or sign Codex in for harness2",
        description="Without an argument, show whether claude-login and chatgpt-login are "
        "ready. `harness2 login codex` signs in to ChatGPT in a Codex home kept for harness2, "
        "apart from your own ~/.codex.",
    )
    p.add_argument("service", nargs="?", choices=("codex",), help="sign this CLI in for harness2")
    p.add_argument(
        "--device",
        action="store_true",
        help="sign in with a device code, for machines without a browser",
    )
    return p


def run(args, ctx) -> int:
    home = providers.codex_login_home(ctx.settings.provider("chatgpt-login"))
    if args.service is None:
        rows = _status(home)
        ctx.out(rows, lambda data: "\n".join(f"{r['provider']:<14} {r['detail']}" for r in data))
        return 0
    if shutil.which("codex") is None:
        print("`codex` is not on PATH; install Codex first.", file=sys.stderr)
        return 1
    if not sys.stdin.isatty():
        print("Signing in needs a terminal and a browser (or --device).", file=sys.stderr)
        return 1
    home.mkdir(parents=True, exist_ok=True)
    print(
        f"Signing Codex in to ChatGPT for harness2. This login lives in {home}, apart from "
        "your own ~/.codex, so your personal hooks and AGENTS.md stay out of harness2's runs.\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
    subprocess.run(
        ["codex", "login", *(["--device-auth"] if args.device else [])],
        env={**env, "CODEX_HOME": str(home)},
    )
    if not providers.codex_logged_in(home):
        print("Codex did not finish signing in.", file=sys.stderr)
        return 1
    print("Signed in. Models written chatgpt-login/<model> now run on this login.")
    return 0


def _status(home) -> list[dict]:
    claude = shutil.which("claude") and _ok(["claude", "auth", "status"])
    codex = providers.codex_logged_in(home)
    return [
        {
            "provider": "claude-login",
            "ready": bool(claude),
            "detail": "ready (Claude Code's own login)"
            if claude
            else "Claude Code is not logged in; run `claude auth login`",
        },
        {
            "provider": "chatgpt-login",
            "ready": codex,
            "detail": f"ready ({home})" if codex else "not signed in; run `harness2 login codex`",
        },
    ]


def _ok(argv: list[str]) -> bool:
    try:
        return subprocess.run(argv, capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
