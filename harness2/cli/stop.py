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

"""Ask a run and its agent processes to stop."""

from __future__ import annotations

import os
import signal

from . import _common as C


def add_parser(subparsers):
    p = subparsers.add_parser("stop", help="stop a running run; rerunning it resumes")
    p.add_argument("name", nargs="?", help="run name (default: the most recent run)")
    return p


def run(args, ctx) -> int:
    root = C.resolve_run(ctx, args.name)
    process = C.run_process(root)
    if process is not None:
        pid = process["pid"]
        try:
            # A detached run leads its own process group, so the agent CLIs it started stop
            # with it. Otherwise the group can include whatever launched the run (a shell, an
            # agent), so only the run itself is signalled.
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGINT)
            else:
                os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            process = None
    ctx.out(
        {
            "name": root.name,
            "pid": process["pid"] if process else None,
            "signalled": process is not None,
        },
        lambda data: (
            f"Asked run {root.name} (pid {data['pid']}) to stop. It ends the calls in flight, "
            "then exits; run the same command again to resume."
            if data["signalled"]
            else f"Run {root.name} is not running."
        ),
    )
    return 0
