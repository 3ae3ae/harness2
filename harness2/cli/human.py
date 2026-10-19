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

"""`--selector human`: a person picks the final output in the terminal.

Only the blind final selection is handed to a person. The candidates are the same anonymised
files an LLM selector would read; the reply uses the selector's format so the method records
the choice exactly as it records a model's.
"""

from __future__ import annotations

import pydoc
import sys
import threading
from pathlib import Path

from ..core.proposer import Proposer, ProposerError, Reply

_TERMINAL = threading.Lock()


class HumanSelector(Proposer):
    """A Proposer whose select sessions are answered by the person at the terminal.

    Domains run in parallel threads; the lock keeps two selections from sharing the prompt.
    """

    def call(self, mission, context_dir, label, **kwargs) -> Reply:
        if not label.startswith("select."):
            raise ProposerError(f"[{label}] the human selector only answers selections")
        if not sys.stdin.isatty():
            raise ProposerError(f"[{label}] --selector human needs an interactive terminal")
        root = Path(context_dir)
        files = sorted((root / "candidates").glob("*.md"))
        ids = [p.stem for p in files]
        with _TERMINAL:
            print(_header(root, label, len(ids)), file=sys.stderr)
            for p in files:
                print(f"  {p.stem}  {_gist(p)}", file=sys.stderr)
            print(f"\nRead them in full: {root / 'candidates'}", file=sys.stderr)
            choice = _ask(ids, files)
        text = f"<why>Chosen by a person.</why>\n```choice\n{choice}\n```"
        reply = Reply(label=label, text=text, ok=True, attempts=1, channel="human")
        self._calls.append(reply)
        return reply


def _header(root: Path, label: str, n: int) -> str:
    task = (root / "task.md").read_text(encoding="utf-8", errors="replace").strip()
    first = task.splitlines()[0][:72] if task else "(no task text)"
    mode = label.removeprefix("select.")
    return f"\nPick the final output — {mode}, {n} candidates, shown anonymously.\nTask: {first}\n"


def _gist(path: Path) -> str:
    """One line per candidate: its size and, for a patch, how many files it touches."""
    body = path.read_text(encoding="utf-8", errors="replace")
    files = body.count("\ndiff --git ") + body.startswith("diff --git ")
    size = f"{body.count(chr(10)) + 1:,} lines"
    return f"{files} file(s) changed · {size}" if files else size


def _ask(ids: list[str], files: list[Path]) -> str:
    choices = "/".join(ids)
    while True:
        try:
            raw = input(f"Choose {choices}, or v<id> to read one here: ").strip()
        except EOFError:
            raise ProposerError("selection cancelled") from None
        if raw in ids:
            return raw
        if raw[:1] == "v" and raw[1:] in ids:
            pydoc.pager(files[ids.index(raw[1:])].read_text(encoding="utf-8", errors="replace"))
            continue
        print(f"Type one of {choices}, or v followed by an id.", file=sys.stderr)
