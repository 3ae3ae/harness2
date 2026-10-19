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

"""Translate provider profiles into each model client's configuration."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .settings import ConfigError, state_dir

PROVIDER_TYPES = ("anthropic", "openai", "vertex", "bedrock", "claude-login", "chatgpt-login")
USAGE_LIMIT = re.compile(
    r"(?i)usage limit|rate[ _-]?limit|too many requests|\b429\b|quota|credit balance is too low"
)
STRIP_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLOUD_ML_REGION",
    "OPENAI_BASE_URL",
)


@dataclass(frozen=True)
class Provider:
    """A named endpoint with credentials referenced by environment variable."""

    name: str
    type: str
    base_url: str = ""
    api_key_env: str = ""
    project: str = ""
    region: str = ""
    home: str = ""


def codex_login_home(p: Provider) -> Path:
    return Path(p.home).expanduser() if p.home else state_dir() / "codex-home"


def codex_logged_in(home: Path) -> bool:
    try:
        return (
            subprocess.run(
                ["codex", "login", "status"],
                env={
                    **{
                        k: v
                        for k, v in os.environ.items()
                        if k not in (*STRIP_ENV, "OPENAI_API_KEY", "CODEX_API_KEY")
                    },
                    "CODEX_HOME": str(home),
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        )
    except Exception:
        return False


def split_model(ref: str) -> tuple[str, str]:
    provider, separator, model = ref.partition("/")
    if not separator or not provider or not model:
        raise ConfigError(
            f"model {ref!r} must be written provider/model, e.g. anthropic/claude-sonnet-5-5"
        )
    return provider, model


def claude_env(p: Provider) -> dict[str, str]:
    if p.type == "claude-login":
        return {}
    if p.type == "chatgpt-login":
        raise ConfigError("Claude Code cannot use a ChatGPT login.")
    if p.type == "anthropic":
        env = {}
        if p.api_key_env:
            env["ANTHROPIC_API_KEY"] = os.environ.get(p.api_key_env, "")
        if p.base_url:
            env["ANTHROPIC_BASE_URL"] = p.base_url
        return env
    if p.type == "vertex":
        return {
            "CLAUDE_CODE_USE_VERTEX": "1",
            "ANTHROPIC_VERTEX_PROJECT_ID": p.project,
            "CLOUD_ML_REGION": p.region or "global",
        }
    if p.type == "bedrock":
        return {"CLAUDE_CODE_USE_BEDROCK": "1", **({"AWS_REGION": p.region} if p.region else {})}
    if p.type == "openai":
        raise ConfigError("Claude Code needs an Anthropic-compatible provider.")
    raise ConfigError(f"unknown provider type {p.type!r}; use one of {PROVIDER_TYPES}")


def codex_config(p: Provider, model: str, effort: str) -> str:
    if p.type != "openai":
        raise ConfigError(
            "Codex needs an OpenAI Responses-compatible provider; serve one with "
            "`harness2 bridge --model <provider>/<model>` and point an openai-type provider at it."
        )

    def quote(value):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")

    # Codex refuses to redefine its built-in provider ids (e.g. "openai"), so namespace ours.
    pid = f"harness2-{p.name}"
    return (
        f"model = {quote(model)}\n"
        f"model_provider = {quote(pid)}\n"
        f"model_reasoning_effort = {quote(effort)}\n\n"
        f"[model_providers.{quote(pid)}]\n"
        f"name = {quote(p.name)}\n"
        f"base_url = {quote(p.base_url)}\n"
        f"env_key = {quote(p.api_key_env)}\n"
        'wire_api = "responses"\n'
    )


def codex_args(p: Provider | None, model: str, effort: str) -> list[str]:
    if p is not None and p.type == "chatgpt-login":
        return [
            "--ignore-user-config",
            "--ignore-rules",
            "-m",
            model,
            "-c",
            f"model_reasoning_effort={json.dumps(effort)}",
        ]
    if p is not None:
        codex_config(p, model, effort)
    return []


def opencode_config(p: Provider, model: str) -> tuple[dict, dict[str, str]]:
    if p.type in ("claude-login", "chatgpt-login"):
        raise ConfigError("OpenCode needs an API provider; subscription logins are not supported.")
    if p.type == "vertex":
        region = p.region or "global"
        return {}, {
            "GOOGLE_VERTEX_PROJECT": p.project,
            "GOOGLE_VERTEX_LOCATION": region,
            "VERTEXAI_LOCATION": region,
        }
    if p.type == "bedrock":
        raise ConfigError("Bedrock is not supported on the opencode channel.")
    if p.type not in ("openai", "anthropic"):
        raise ConfigError(f"unknown provider type {p.type!r}; use one of {PROVIDER_TYPES}")
    options = {}
    if p.api_key_env or p.type == "openai":
        options["apiKey"] = "{env:" + p.api_key_env + "}"
    if p.base_url or p.type == "openai":
        options["baseURL"] = p.base_url
    npm = "@ai-sdk/openai-compatible" if p.type == "openai" else "@ai-sdk/anthropic"
    return {
        "provider": {
            p.name: {
                "npm": npm,
                "name": p.name,
                "options": options,
                "models": {model: {}},
            }
        }
    }, {}


def litellm_params(p: Provider, model: str) -> dict:
    if p.type in ("claude-login", "chatgpt-login"):
        raise ConfigError(
            "The bridge needs an API provider; subscription logins are not supported."
        )
    if p.type in ("anthropic", "openai"):
        params = {"model": f"{p.type}/{model}", "api_key": f"os.environ/{p.api_key_env}"}
        if p.base_url or p.type == "openai":
            params["api_base"] = p.base_url
        return params
    if p.type == "vertex":
        return {
            "model": f"vertex_ai/{model}",
            "vertex_project": p.project,
            "vertex_location": p.region or "global",
        }
    if p.type == "bedrock":
        return {"model": f"bedrock/{model}", "aws_region_name": p.region}
    raise ConfigError(f"unknown provider type {p.type!r}; use one of {PROVIDER_TYPES}")


def check(p: Provider) -> list[str]:
    if p.type == "chatgpt-login":
        home = codex_login_home(p)
        if codex_logged_in(home):
            return []
        if p.home:
            return [
                f"chatgpt-login: not logged in at {home}; run `codex login` with CODEX_HOME={home}"
            ]
        return ["chatgpt-login: not logged in for harness2; run `harness2 login codex`"]
    if p.type == "claude-login":
        try:
            result = subprocess.run(
                ["claude", "auth", "status"],
                env={k: v for k, v in os.environ.items() if k not in STRIP_ENV},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if result.returncode == 0:
                return []
        except (OSError, subprocess.SubprocessError):
            pass
        return ["claude-login: not logged in; run `claude auth login`"]
    problems = []
    if p.api_key_env and not os.environ.get(p.api_key_env):
        problems.append(f"{p.name}: set {p.api_key_env}")
    if p.type == "vertex" and not p.project:
        problems.append(f"{p.name}: set project")
    if p.base_url:
        try:
            url = urlsplit(p.base_url)
            valid = url.scheme in ("http", "https") and bool(url.netloc)
        except ValueError:
            valid = False
        if not valid:
            problems.append(f"{p.name}: base_url must be an HTTP(S) URL")
    return problems
