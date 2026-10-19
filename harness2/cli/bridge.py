# Copyright 2026 Google LLC
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

"""Serve a Responses-API endpoint for a configured provider/model."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
from pathlib import Path

from harness2 import providers, settings
from harness2.settings import ConfigError


def _free_port(host: str) -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _endpoint(path: Path, host: str, port: int) -> dict:
    """Reuse the saved port and key across bridge invocations."""
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    endpoint = {
        "port": port or saved.get("port") or _free_port(host),
        "key": saved.get("key") or secrets.token_urlsafe(24),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(endpoint) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return endpoint


def _arguments(p):
    p.add_argument("--model", required=True, help="provider/model to serve")
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to listen on; use 0.0.0.0 when containers must reach it",
    )
    p.add_argument("--port", type=int, default=0, help="port (default: the saved one, or free)")
    return p


def add_parser(subparsers):
    return _arguments(
        subparsers.add_parser(
            "bridge",
            help="serve a model as a Responses API endpoint for Codex",
            description="Serve any configured provider/model as an OpenAI Responses API "
            "endpoint through LiteLLM, so Codex can run on it. Needs the bridge extra: "
            "pip install 'harness2-cli[bridge]'.",
        )
    )


def run(args, ctx):
    litellm = Path(sys.executable).with_name("litellm")
    if not litellm.is_file():
        raise ConfigError("LiteLLM is not installed; run: uv pip install -e '.[bridge]'")
    config = ctx.settings
    provider_name, model = providers.split_model(args.model)
    provider = config.provider(provider_name)
    params = providers.litellm_params(provider, model)

    root = settings.cache_dir() / "bridge"
    endpoint = _endpoint(root / "bridge.json", args.host, args.port)
    port = endpoint["port"]
    litellm_config = root / "bridge.yaml"
    # JSON is valid YAML and safely preserves arbitrary model and endpoint strings.
    litellm_config.write_text(
        json.dumps(
            {
                "model_list": [{"model_name": model, "litellm_params": params}],
                "litellm_settings": {"drop_params": True},
                "general_settings": {"master_key": "os.environ/HARNESS2_BRIDGE_KEY"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    snippet = (
        "[providers.bridge]\n"
        'type = "openai"\n'
        f'base_url = "http://127.0.0.1:{port}/v1"\n'
        'api_key_env = "HARNESS2_BRIDGE_KEY"'
    )
    ctx.out(
        {"provider": snippet, "environment": {"HARNESS2_BRIDGE_KEY": endpoint["key"]}},
        lambda data: (
            f"Serving {args.model} on port {port}. Add this provider to .harness2/config.toml "
            f"and use the model as bridge/{model}:\n\n"
            + data["provider"]
            + "\n\nand put its key in .harness2/.env:\n\nHARNESS2_BRIDGE_KEY="
            + data["environment"]["HARNESS2_BRIDGE_KEY"]
            + "\n"
        ),
    )
    sys.stdout.flush()

    env = {**os.environ, "HARNESS2_BRIDGE_KEY": endpoint["key"]}
    command = [str(litellm), "--config", str(litellm_config), "--host", args.host]
    os.execve(str(litellm), [*command, "--port", str(port)], env)
    return 0


def main(argv=None):
    from ._common import Context

    p = _arguments(argparse.ArgumentParser())
    args = p.parse_args(argv)
    args.project, args.profile, args.config = None, None, []
    args.json, args.quiet, args.verbose = False, False, False
    try:
        return run(args, Context(args, {}))
    except ConfigError as exc:
        p.exit(1, f"{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
