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

"""Provider translations without network or model calls."""

from __future__ import annotations

import os
import tomllib
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from harness2 import providers
from harness2.providers import Provider
from harness2.settings import ConfigError


class ProviderTests(unittest.TestCase):
    def test_model_split(self):
        self.assertEqual(providers.split_model("anthropic/claude-x"), ("anthropic", "claude-x"))
        self.assertEqual(providers.split_model("local/team/model"), ("local", "team/model"))
        for ref in ("bare", "", "/model", "provider/"):
            with self.subTest(ref=ref), self.assertRaises(ConfigError) as caught:
                providers.split_model(ref)
            self.assertEqual(
                str(caught.exception),
                (f"model {ref!r} must be written provider/model, e.g. anthropic/claude-sonnet-5-5"),
            )

    def test_provider_is_frozen_and_strip_env_is_exact(self):
        provider = Provider("custom", "anthropic")
        with self.assertRaises(FrozenInstanceError):
            provider.region = "changed"
        self.assertEqual(
            providers.PROVIDER_TYPES,
            ("anthropic", "openai", "vertex", "bedrock", "claude-login", "chatgpt-login"),
        )
        self.assertEqual(
            providers.STRIP_ENV,
            (
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
            ),
        )

    def test_claude_anthropic_env_and_optional_values(self):
        with patch.dict(os.environ, {"CUSTOM_KEY": "secret"}, clear=True):
            self.assertEqual(
                providers.claude_env(
                    Provider(
                        "custom",
                        "anthropic",
                        "https://example.com",
                        "CUSTOM_KEY",
                    )
                ),
                {"ANTHROPIC_API_KEY": "secret", "ANTHROPIC_BASE_URL": "https://example.com"},
            )
            self.assertEqual(providers.claude_env(Provider("a", "anthropic")), {})
            self.assertEqual(
                providers.claude_env(Provider("a", "anthropic", api_key_env="MISSING")),
                {
                    "ANTHROPIC_API_KEY": "",
                },
            )

    def test_claude_vertex_and_bedrock_env(self):
        self.assertEqual(
            providers.claude_env(Provider("v", "vertex", project="project")),
            {
                "CLAUDE_CODE_USE_VERTEX": "1",
                "ANTHROPIC_VERTEX_PROJECT_ID": "project",
                "CLOUD_ML_REGION": "global",
            },
        )
        self.assertEqual(
            providers.claude_env(
                Provider(
                    "v",
                    "vertex",
                    project="project",
                    region="us-east1",
                )
            )["CLOUD_ML_REGION"],
            "us-east1",
        )
        self.assertEqual(
            providers.claude_env(Provider("b", "bedrock")),
            {
                "CLAUDE_CODE_USE_BEDROCK": "1",
            },
        )
        self.assertEqual(
            providers.claude_env(Provider("b", "bedrock", region="us-east-1")),
            {
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "AWS_REGION": "us-east-1",
            },
        )
        with self.assertRaisesRegex(
            ConfigError, "Claude Code needs an Anthropic-compatible provider"
        ):
            providers.claude_env(Provider("o", "openai"))

    def test_codex_config_is_valid_toml_and_escapes_values(self):
        provider = Provider('local."name 😀', "openai", "http://localhost:8000/v1", "LOCAL_KEY")
        parsed = tomllib.loads(providers.codex_config(provider, 'model"\nnext 😀\x7f', "high"))
        self.assertEqual(
            parsed,
            {
                "model": 'model"\nnext 😀\x7f',
                "model_provider": f"harness2-{provider.name}",
                "model_reasoning_effort": "high",
                "model_providers": {
                    f"harness2-{provider.name}": {
                        "name": provider.name,
                        "base_url": provider.base_url,
                        "env_key": "LOCAL_KEY",
                        "wire_api": "responses",
                    }
                },
            },
        )
        for kind in ("anthropic", "vertex", "bedrock"):
            with self.subTest(kind=kind), self.assertRaises(ConfigError) as caught:
                providers.codex_config(Provider("p", kind), "model", "high")
            self.assertEqual(
                str(caught.exception),
                (
                    "Codex needs an OpenAI Responses-compatible provider; serve one with "
                    "`harness2 bridge --model <provider>/<model>` and point an openai-type "
                    "provider at it."
                ),
            )

    def test_opencode_openai_and_anthropic(self):
        for kind, npm in (
            ("openai", "@ai-sdk/openai-compatible"),
            ("anthropic", "@ai-sdk/anthropic"),
        ):
            with self.subTest(kind=kind):
                provider = Provider("local", kind, "http://localhost:8000", "MY_KEY")
                self.assertEqual(
                    providers.opencode_config(provider, "model"),
                    (
                        {
                            "provider": {
                                "local": {
                                    "npm": npm,
                                    "name": "local",
                                    "options": {
                                        "baseURL": "http://localhost:8000",
                                        "apiKey": "{env:MY_KEY}",
                                    },
                                    "models": {"model": {}},
                                }
                            }
                        },
                        {},
                    ),
                )
        self.assertEqual(
            providers.opencode_config(Provider("a", "anthropic"), "model")[0],
            {
                "provider": {
                    "a": {
                        "npm": "@ai-sdk/anthropic",
                        "name": "a",
                        "options": {},
                        "models": {"model": {}},
                    }
                },
            },
        )
        self.assertEqual(
            providers.opencode_config(Provider("o", "openai"), "model")[0]["provider"]["o"][
                "options"
            ],
            {"baseURL": "", "apiKey": "{env:}"},
        )

    def test_opencode_vertex_and_bedrock(self):
        for region in ("", "europe-west1"):
            with self.subTest(region=region):
                self.assertEqual(
                    providers.opencode_config(
                        Provider(
                            "v",
                            "vertex",
                            project="project",
                            region=region,
                        ),
                        "model",
                    ),
                    (
                        {},
                        {
                            "GOOGLE_VERTEX_PROJECT": "project",
                            "GOOGLE_VERTEX_LOCATION": region or "global",
                            "VERTEXAI_LOCATION": region or "global",
                        },
                    ),
                )
        with self.assertRaisesRegex(
            ConfigError, "Bedrock is not supported on the opencode channel"
        ):
            providers.opencode_config(Provider("b", "bedrock"), "model")

    def test_litellm_params_for_each_type(self):
        for kind in ("anthropic", "openai"):
            with self.subTest(kind=kind):
                self.assertEqual(
                    providers.litellm_params(
                        Provider(
                            "local",
                            kind,
                            "https://example.com",
                            "CUSTOM_KEY",
                        ),
                        "model",
                    ),
                    {
                        "model": f"{kind}/model",
                        "api_key": "os.environ/CUSTOM_KEY",
                        "api_base": "https://example.com",
                    },
                )
                self.assertEqual(
                    providers.litellm_params(Provider("local", kind), "model"),
                    {
                        "model": f"{kind}/model",
                        "api_key": "os.environ/",
                        **({"api_base": ""} if kind == "openai" else {}),
                    },
                )
        self.assertEqual(
            providers.litellm_params(
                Provider(
                    "v",
                    "vertex",
                    project="project",
                    region="us-east1",
                ),
                "model",
            ),
            {
                "model": "vertex_ai/model",
                "vertex_project": "project",
                "vertex_location": "us-east1",
            },
        )
        self.assertEqual(
            providers.litellm_params(Provider("v", "vertex"), "model")["vertex_location"], "global"
        )
        self.assertEqual(
            providers.litellm_params(Provider("b", "bedrock", region="us-west-2"), "model"),
            {
                "model": "bedrock/model",
                "aws_region_name": "us-west-2",
            },
        )

    def test_unknown_provider_types_raise_config_error(self):
        for function, args in (
            (providers.claude_env, ()),
            (providers.codex_config, ("model", "high")),
            (providers.opencode_config, ("model",)),
            (providers.litellm_params, ("model",)),
        ):
            with self.subTest(function=function.__name__), self.assertRaises(ConfigError):
                function(Provider("p", "unknown"), *args)

    def test_doctor_checks_missing_env_project_and_invalid_urls(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = Provider("v", "vertex", "ftp://example.com", "CUSTOM_KEY")
            self.assertEqual(
                providers.check(provider),
                [
                    "v: set CUSTOM_KEY",
                    "v: set project",
                    "v: base_url must be an HTTP(S) URL",
                ],
            )
            os.environ["CUSTOM_KEY"] = ""
            self.assertEqual(
                providers.check(Provider("a", "anthropic", api_key_env="CUSTOM_KEY")),
                [
                    "a: set CUSTOM_KEY",
                ],
            )
            os.environ["CUSTOM_KEY"] = "present"
            self.assertEqual(
                providers.check(Provider("a", "anthropic", api_key_env="CUSTOM_KEY")), []
            )
        for url in ("https://example.com/v1", "http://localhost:8080", ""):
            with self.subTest(url=url):
                self.assertEqual(providers.check(Provider("o", "openai", url)), [])
        for url in ("file:///tmp/endpoint", "localhost:8080", "https://", "http://[bad"):
            with self.subTest(url=url):
                self.assertEqual(
                    providers.check(Provider("o", "openai", url)),
                    [
                        "o: base_url must be an HTTP(S) URL",
                    ],
                )


if __name__ == "__main__":
    unittest.main()
