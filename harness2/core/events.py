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

"""Thread-safe runtime events and resumable spending limits."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class Stopped(RuntimeError):
    """Execution stopped at a user request or a spending limit."""


_lock = threading.Lock()
_active = False
_path: Path | None = None
_spent_usd = 0.0
_calls = 0
_budget_usd = 0.0
_max_calls = 0
_stopped: str | None = None


def _accumulate(record: dict) -> None:
    global _spent_usd, _calls
    _spent_usd += record.get("cost_usd", 0) or 0
    if record["kind"] == "rollout" or (record["kind"] == "session" and not record.get("cached")):
        _calls += 1


def start(path: Path | None, *, budget_usd: float = 0.0, max_calls: int = 0) -> None:
    """Reset limits and restore accounting from a previous event stream."""
    if budget_usd < 0 or max_calls < 0:
        raise ValueError("budget_usd and max_calls must be non-negative")
    global _active, _path, _spent_usd, _calls, _budget_usd, _max_calls, _stopped
    with _lock:
        _active = True
        _path = Path(path) if path is not None else None
        _spent_usd, _calls = 0.0, 0
        _budget_usd, _max_calls, _stopped = budget_usd, max_calls, None
        if _path is not None:
            _path.parent.mkdir(parents=True, exist_ok=True)
            if _path.is_file():
                for line in _path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        _accumulate(json.loads(line))


def emit(kind: str, **fields) -> None:
    """Append one event and count completed work, under the same lock."""
    with _lock:
        if not _active:
            return
        record = {
            "t": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "kind": kind,
            **fields,
        }
        if _path is not None:
            with _path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")
        _accumulate(record)


def _reason() -> str | None:
    if _stopped:
        return _stopped
    if _active and _budget_usd and _spent_usd >= _budget_usd:
        return "budget_usd"
    if _active and _max_calls and _calls >= _max_calls:
        return "max_calls"
    return None


def check() -> None:
    """Raise before starting more work when a limit or stop request applies."""
    with _lock:
        reason = _reason()
    if reason is not None:
        raise Stopped(reason)


def request_stop(reason: str) -> None:
    global _stopped
    with _lock:
        _stopped = reason


def totals() -> dict:
    with _lock:
        return {
            "spent_usd": round(_spent_usd, 6),
            "calls": _calls,
            "budget_usd": _budget_usd,
            "max_calls": _max_calls,
            "stopped": _reason(),
        }
