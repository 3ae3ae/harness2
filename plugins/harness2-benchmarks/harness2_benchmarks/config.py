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

"""Deployment settings and dependency versions. Shell values override .env."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from harness2.core.spec import ChannelSpec
from harness2.providers import Provider
from harness2.settings import ConfigError, cache_dir
from harness2.settings import load_dotenv as load_dotenv

PKG = Path(__file__).resolve().parent
THIRD_PARTY = PKG / "third_party"
METHOD_VERSION = 2
DEPENDENCIES = (
    Path(os.environ.get("HARNESS2_BENCH_DEPS") or cache_dir() / "benchmarks").expanduser().resolve()
)


def env(name: str, default=None):
    """Env override where a set-but-empty variable is the same as an unset one."""
    return os.environ.get(name) or default


def env_int(name: str, default: int) -> int:
    v = env(name)
    if v is None:
        return int(default)
    try:
        return int(v)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer, got {v!r}") from e


def require(name: str, *, needed_by: str) -> str:
    """The value of `name`, or ConfigError with the remediation line when it is unset/empty."""
    v = env(name)
    if not v:
        raise ConfigError(
            f"{name} is required for {needed_by}; set it in .harness2/.env or the shell (see .env.example)"
        )
    return str(v)


VERTEX_PROJECT = env("GOOGLE_CLOUD_PROJECT", env("HARNESS2_VERTEX_PROJECT", ""))
VERTEX_REGION = env("GOOGLE_CLOUD_LOCATION", "global")
if VERTEX_PROJECT:
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", VERTEX_PROJECT)
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", VERTEX_REGION)


SOLVER_MODEL_OC = env("HARNESS2_SOLVER_MODEL", "")
SOLVER_MODEL_CX = env("HARNESS2_CODEX_MODEL", SOLVER_MODEL_OC.split("/", 1)[-1])
IMPROVER_MODEL = env("HARNESS2_IMPROVER_MODEL", SOLVER_MODEL_OC)
JUDGE_MODEL = env("HARNESS2_JUDGE_MODEL", "")

DEPENDENCY_PINS = {
    "job-bench-eval": "fa03a17f9ff662f136798a274e10011c3aa00fd0",
    "workbuddy-bench": "b516950be5b56eb3be406c2f76ee1c5111dcb57f",
    "harvey-labs": "b58a28ae9fde5ee4b810e53b4fd0a1915325a35d",
    "opencode": "23fb5e0516c99ac04a1aa46c193efda2e1b9bb24",
}
DEPENDENCY_ORIGINS = {
    "job-bench-eval": "https://github.com/Job-Bench/job-bench-eval.git",
    "workbuddy-bench": "https://github.com/Tencent/workbuddy-bench.git",
    "harvey-labs": "https://github.com/harveyai/harvey-labs.git",
    "opencode": "https://github.com/anomalyco/opencode.git",
}


def jb_repo() -> Path:
    return DEPENDENCIES / "job-bench-eval"


def wb_repo() -> Path:
    return DEPENDENCIES / "workbuddy-bench"


def lab_repo() -> Path:
    return DEPENDENCIES / "harvey-labs"


OPENCODE_DIR = DEPENDENCIES / "opencode"
BUN = DEPENDENCIES / "tools" / "node_modules" / ".bin" / "bun"
LAB_PYTHON = lab_repo() / ".venv" / "bin" / "python"
LAB_JUDGE_MODEL = (
    "vertex/" + JUDGE_MODEL if JUDGE_MODEL.startswith(("claude", "gemini")) else JUDGE_MODEL
)
LAB_DELIV_CAP = 60000
JB_JUDGE_PYTHON = jb_repo() / ".venv" / "bin" / "python"
JB_JUDGE_MODEL = (
    "google/" + JUDGE_MODEL
    if JUDGE_MODEL.startswith("gemini") and not env("HARNESS2_JUDGE_BASE_URL")
    else JUDGE_MODEL
)
_vertex_host = (
    "aiplatform.googleapis.com"
    if VERTEX_REGION == "global"
    else f"{VERTEX_REGION}-aiplatform.googleapis.com"
)
VERTEX_OPENAI_URL = (
    f"https://{_vertex_host}/v1/projects/{VERTEX_PROJECT}"
    f"/locations/{VERTEX_REGION}/endpoints/openapi"
)
JB_JUDGE_API_BASE = env("HARNESS2_JUDGE_BASE_URL", VERTEX_OPENAI_URL if VERTEX_PROJECT else "")
JB_JUDGE_API_KEY = env("HARNESS2_JUDGE_API_KEY", "")
JB_JUDGE_REASONING_EFFORT = "low"
JB_REPORTING_JUDGE_DETAILS = ""
JB_OBS_TRUNC = 50000

CODEX_VERSION = "0.144.6"
CODEX_DIST = str(DEPENDENCIES / "codex" / "dist")
CODEX_BASE_URL = env("HARNESS2_CODEX_BASE_URL", "")
CODEX_CONTAINER_URL = env("HARNESS2_CODEX_CONTAINER_URL", CODEX_BASE_URL)
VERTEX_PROXY_KEY = env("HARNESS2_CODEX_API_KEY", "")
CODEX_HOME_DIR = DEPENDENCIES / "codex" / "home"

WB_SOLVER_MODEL = "harness2/solver"
WB_WEB_JUDGE = "harness2/judge"
WB_RENDER_PY = sys.executable
WB_OC_HARNESS_VER = "1.14.18"
WB_CX_HARNESS_VER = CODEX_VERSION
WB_MAX_JOBS = {"oc": 1, "cx": 1}
WB_MAX_TRIALS = {"oc": 2, "cx": 2}
WB_MODEL_CONNECTION = "local_proxy"

DEFAULT_PROPOSER_CHANNEL = "opencode"
PROPOSER_MODEL_BY_CHANNEL = {"opencode": IMPROVER_MODEL, "claude_cli": IMPROVER_MODEL}
PHASE_WIDTH = 2
TRANSPORT_ATTEMPTS = 2
PROPOSER_CONCURRENCY = 2


def validate_models(*, judge: bool = False) -> None:
    require("HARNESS2_SOLVER_MODEL", needed_by="executor rollouts")
    if "/" not in SOLVER_MODEL_OC or "/" not in IMPROVER_MODEL:
        raise ConfigError("Executor and improver models must use OpenCode's provider/model syntax")
    if any(m.startswith("google-vertex") for m in (SOLVER_MODEL_OC, IMPROVER_MODEL)):
        if not VERTEX_PROJECT:
            raise ConfigError("Set GOOGLE_CLOUD_PROJECT for Vertex AI")
    if judge:
        require("HARNESS2_JUDGE_MODEL", needed_by="benchmark judging")


CHANNELS = ("claude_cli", "opencode")


def resolve_channel_model(
    kind: str | None,
    model: str | None,
    *,
    effort: str = "high",
    steps: int = 120,
    timeout: int = 3600,
) -> ChannelSpec:
    """The one channel->model mapping; a lone override of either raises."""
    kwargs = {"effort": effort, "steps": steps, "timeout": timeout}
    if (kind or DEFAULT_PROPOSER_CHANNEL) == "opencode":
        kwargs["bin"] = (
            f"{BUN} run --cwd {OPENCODE_DIR}/packages/opencode --conditions=browser src/index.ts"
        )
    if VERTEX_PROJECT:
        kwargs["provider"] = Provider(
            "vertex", "vertex", project=VERTEX_PROJECT, region=VERTEX_REGION
        )
    use_default = kind is None and model is None
    if (kind is None) != (model is None):
        raise ValueError(
            "channel and model must be overridden TOGETHER (a bare-id/provider-prefixed "
            f"mismatch fails silently downstream); got kind={kind!r}, model={model!r}"
        )
    if kind is None:
        kind = DEFAULT_PROPOSER_CHANNEL
    if kind not in CHANNELS:
        raise ValueError(f"unknown channel {kind!r}; expected one of {CHANNELS}")
    if model is None:
        model = PROPOSER_MODEL_BY_CHANNEL[kind]
    if not model.strip():
        if use_default:
            return ChannelSpec(kind, "", **kwargs)
        raise ValueError("model override must not be empty")
    if kind == "opencode" and "/" not in model:
        raise ValueError(
            f"opencode channel needs a provider-prefixed model id "
            f"in HARNESS2_IMPROVER_MODEL or HARNESS2_SOLVER_MODEL, got {model!r}"
        )
    if kind == "claude_cli" and "/" in model:
        raise ValueError(
            f"claude_cli channel takes a bare Anthropic id "
            f"(e.g. {PROPOSER_MODEL_BY_CHANNEL['claude_cli']!r}), got {model!r}"
        )
    return ChannelSpec(kind, model, **kwargs)
