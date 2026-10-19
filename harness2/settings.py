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

"""Layered project settings, provenance, and TOML edits."""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import providers

DEFAULTS = {
    "suite": {"name": "tasks", "work": ""},
    "paths": {"harness": ".", "tasks": ".harness2/tasks", "runs": ".harness2/runs"},
    "executor": {
        "name": "claude",
        "model": "",
        "bin": "",
        "effort": "high",
        "timeout": 1800,
        "command": "",
        "editable": [],
    },
    "improver": {
        "channel": "claude",
        "model": "",
        "bin": "",
        "effort": "high",
        "timeout": 3600,
        "steps": 120,
        "steer": "",
    },
    "selector": {},
    "reflector": {},
    "judge": {},
    "run": {
        "k": 1,
        "modes": "parallel",
        "seed": None,
        "workers": 1,
        "phase_width": 2,
        "proposer_concurrency": 2,
        "transport_attempts": 2,
        "obs_trunc": 50000,
        "budget_usd": 0.0,
        "max_calls": 0,
    },
    "providers": {
        "anthropic": {"type": "anthropic", "base_url": "", "api_key_env": "ANTHROPIC_API_KEY"},
        "openai": {
            "type": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "OPENAI_API_KEY",
        },
        "claude-login": {"type": "claude-login"},
        "chatgpt-login": {"type": "chatgpt-login"},
    },
}
_MISSING = object()


class ConfigError(RuntimeError):
    """Invalid configuration; each message line carries the remediation."""


def find_project(start: Path) -> Path | None:
    """Find the nearest ancestor, including start, with a .harness2 directory."""
    start = start.resolve()
    return next((p for p in (start, *start.parents) if (p / ".harness2").is_dir()), None)


def global_path() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "harness2/config.toml"
    )


def project_path(project: Path) -> Path:
    return project / ".harness2/config.toml"


def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "harness2"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "harness2"


def parse_value(raw: str) -> Any:
    """Parse a TOML literal, falling back to an unquoted string."""
    try:
        return tomllib.loads("v = " + raw)["v"]
    except (tomllib.TOMLDecodeError, KeyError):
        return raw


def _merge(data: dict, values: Mapping, origins: dict[str, str], origin: str, prefix="") -> None:
    for key, value in values.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            if not isinstance(data.get(key), dict):
                data[key] = {}
            _merge(data[key], value, origins, origin, dotted)
        else:
            data[key] = deepcopy(value)
        origins[dotted] = origin


def _put(data: dict, key: str, value: Any, origins: dict[str, str], origin: str) -> None:
    parts = key.split(".")
    if not all(parts):
        raise ConfigError(f"invalid setting key {key!r}; use a dotted key")
    nested = value
    for part in reversed(parts):
        nested = {part: nested}
    _merge(data, nested, origins, origin)


def _validate(data: dict) -> None:
    from . import providers  # providers imports ConfigError from here

    problems = []

    def secrets(value):
        if isinstance(value, dict):
            if "api_key" in value:
                problems.append(
                    "store secrets in environment variables and reference them with api_key_env"
                )
            for child in value.values():
                secrets(child)
        elif isinstance(value, list):
            for child in value:
                secrets(child)

    secrets(data)
    for name, value in data.items():
        if name not in DEFAULTS and name != "profiles":
            problems.append(
                f"unknown section {name!r}; use one of {', '.join(DEFAULTS)} or profiles"
            )
        elif not isinstance(value, dict):
            problems.append(f"{name} must be a table")
    run = data.get("run", {})
    if isinstance(run, dict):
        for key in ("k", "workers", "phase_width", "proposer_concurrency", "transport_attempts"):
            value = run.get(key)
            if type(value) is not int or value <= 0:
                problems.append(f"run.{key} must be a positive integer")
        for key in ("budget_usd", "max_calls"):
            value = run.get(key)
            if type(value) not in (int, float) or not value >= 0:
                problems.append(f"run.{key} must be a non-negative number")
    configured = data.get("providers", {})
    if isinstance(configured, dict):
        for name, value in configured.items():
            if not isinstance(value, dict):
                problems.append(f"providers.{name} must be a table")
            elif value.get("type") not in providers.PROVIDER_TYPES:
                problems.append(
                    f"providers.{name}.type must be one of {', '.join(providers.PROVIDER_TYPES)}"
                )
    if problems:
        raise ConfigError("\n".join(dict.fromkeys(problems)))


@dataclass
class Settings:
    """Resolved settings and the source of each stored value."""

    project: Path | None
    _data: dict = field(repr=False)
    _origins: dict[str, str] = field(repr=False)

    def get(self, key: str, default=_MISSING) -> Any:
        value = self._data
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                if default is _MISSING:
                    raise KeyError(key)
                return default
            value = value[part]
        return deepcopy(value)

    def origin(self, key: str) -> str:
        self.get(key)
        return self._origins[key]

    def flat(self) -> dict[str, Any]:
        values = {}

        def visit(data, prefix=""):
            for key, value in data.items():
                dotted = f"{prefix}.{key}" if prefix else key
                if isinstance(value, dict):
                    visit(value, dotted)
                else:
                    values[dotted] = deepcopy(value)

        visit(self._data)
        return values

    def section(self, name: str) -> dict:
        return self.get(name)

    def role(self, name: str) -> dict:
        if name not in ("improver", "selector", "reflector", "judge"):
            raise ConfigError(f"unknown role {name!r}; use improver, selector, reflector or judge")
        values = self.section("improver")
        if name == "reflector":
            selector = self.role("selector")
            if selector["channel"] != "human":
                values.update(selector)
        if name != "improver":
            values.update(self.section(name))
        keys = ("channel", "model", "bin", "effort", "timeout", "steps")
        if name == "improver":
            keys += ("steer",)
        return {key: values[key] for key in keys}

    def provider(self, name: str) -> providers.Provider:
        from . import providers

        configured = self.section("providers")
        if name not in configured:
            raise ConfigError(f"unknown provider {name!r}; configure providers.{name}")
        fields = ("type", "base_url", "api_key_env", "project", "region", "home")
        return providers.Provider(
            name, **{k: v for k, v in configured[name].items() if k in fields}
        )

    def path(self, key: str) -> Path:
        path = Path(self.get(key)).expanduser()
        return ((self.project or Path.cwd()) / path).resolve()


def load(
    project: Path | None,
    *,
    profile: str | None = None,
    overrides: Mapping[str, tuple[Any, str]] = {},
) -> Settings:
    """Apply defaults, files, a profile, environment, and explicit overrides in order."""
    if project is not None:
        project = project.resolve()
        load_dotenv(project / ".harness2/.env")
    data, origins = {}, {}
    _merge(data, DEFAULTS, origins, "default")
    paths = [global_path()]
    if project is not None:
        paths.append(project_path(project))
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as e:
            raise ConfigError(f"cannot read {path}: {e}") from e
        try:
            values = tomllib.loads(content)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"invalid TOML in {path}: {e}") from e
        _merge(data, values, origins, str(path))
    profile = profile if profile is not None else os.environ.get("HARNESS2_PROFILE")
    if profile:
        profiles = data.get("profiles", {})
        if not isinstance(profiles, dict) or profile not in profiles:
            raise ConfigError(f"unknown profile {profile!r}; configure profiles.{profile}")
        if not isinstance(profiles[profile], dict):
            raise ConfigError(f"profiles.{profile} must be a table")
        _merge(data, deepcopy(profiles[profile]), origins, f"profile {profile}")
    for name, raw in os.environ.items():
        if name.startswith("HARNESS2_") and "__" in name:
            key = name.removeprefix("HARNESS2_").lower().replace("__", ".")
            _put(data, key, parse_value(raw), origins, f"env {name}")
    for key, (value, origin) in overrides.items():
        _put(data, key, value, origins, origin)
    _validate(data)
    return Settings(project, data, origins)


def _literal(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, bool):
        return str(value).lower()
    if type(value) in (int, float):
        return str(value)
    if isinstance(value, list) and all(not isinstance(v, list) for v in value):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    raise ConfigError("config values must be strings, booleans, numbers or lists of scalars")


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _literal(key)


def _comments(text: str) -> list[str]:
    comments = []
    quote = ""
    i = 0
    while i < len(text):
        if quote:
            if text[i] == "\\" and quote.startswith('"'):
                i += 2
                continue
            if text.startswith(quote, i):
                i += len(quote)
                if len(quote) == 3:
                    while i < len(text) and text[i] == quote[0]:
                        i += 1
                quote = ""
                continue
        elif text[i] in "\"'":
            quote = text[i] * (3 if text.startswith(text[i] * 3, i) else 1)
            i += len(quote)
            continue
        elif text[i] == "#":
            end = text.find("\n", i)
            end = len(text) if end == -1 else end
            comments.append(text[i:end].rstrip("\r"))
            i = end
        i += 1
    return comments


def set_value(path: Path, key: str, value: Any) -> None:
    """Replace or append a TOML assignment while retaining unrelated lines and comments."""
    parts = key.split(".")
    if not all(parts):
        raise ConfigError(f"invalid setting key {key!r}; use a dotted key")
    literal = _literal(value)
    try:
        with path.open(encoding="utf-8", newline="") as source:
            text = source.read()
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from e
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    table, leaf = tuple(parts[:-1]), parts[-1]
    current = ()
    insert = 0 if not table else None
    start = 0
    while start < len(lines):
        end = start + 1
        while True:
            try:
                parsed = tomllib.loads("".join(lines[start:end]))
                break
            except tomllib.TOMLDecodeError:
                end += 1
                if end > len(lines):
                    raise ConfigError(f"cannot edit {key!r} in {path}") from None
        if lines[start].lstrip().startswith("["):
            names = []
            node = parsed
            while isinstance(node, dict) and len(node) == 1:
                name, node = next(iter(node.items()))
                names.append(name)
            current = tuple(names) if isinstance(node, dict) else None
        if current == table:
            insert = end
            if leaf in parsed and not lines[start].lstrip().startswith("["):
                comments = _comments("".join(lines[start:end]))
                indent = lines[start][: len(lines[start]) - len(lines[start].lstrip())]
                replacement = f"{indent}{_toml_key(leaf)} = {literal}"
                if comments:
                    replacement += " " + comments.pop()
                replacement += newline
                lines[start:end] = [*(c + newline for c in comments), replacement]
                break
        start = end
    else:
        assignment = f"{_toml_key(leaf)} = {literal}{newline}"
        if insert is None:
            header = ".".join(_toml_key(part) for part in table)
            lines.append(f"{newline}[{header}]{newline}{assignment}")
        else:
            if insert and not lines[insert - 1].endswith("\n"):
                assignment = newline + assignment
            lines.insert(insert, assignment)
    result = "".join(lines)
    try:
        tomllib.loads(result)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"cannot set {key!r} in {path}: {e}") from e
    try:
        encoded = result.encode("utf-8")
    except UnicodeError as e:
        raise ConfigError("config strings must contain valid Unicode") from e
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines with setdefault, using the original config module's parser."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not all(c.isalnum() or c == "_" for c in key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)
