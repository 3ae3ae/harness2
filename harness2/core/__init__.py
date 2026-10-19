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

"""Substrate- and benchmark-independent method code.

Per-task harness adaptation over a c1..c8 component tree, two recursion modes at an identical
budget, blind answer-free selection, and a per-domain experience library.
"""

from __future__ import annotations

import threading


def configure(*, phase_width=None, transport_attempts=None, proposer_concurrency=None) -> None:
    """Apply runtime scheduling settings before starting work."""
    from . import proposer, recursion, schedule

    values = {
        "phase_width": phase_width,
        "transport_attempts": transport_attempts,
        "proposer_concurrency": proposer_concurrency,
    }
    for name, value in values.items():
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError(f"{name} must be a positive integer")
    if phase_width is not None:
        recursion.PHASE_WIDTH = schedule.ROLLOUT_PARALLEL = phase_width
    if transport_attempts is not None:
        recursion.TRANSPORT_ATTEMPTS = transport_attempts
    if proposer_concurrency is not None:
        proposer.MAX_CONCURRENCY = proposer_concurrency
        proposer._SEM = threading.BoundedSemaphore(proposer_concurrency)
