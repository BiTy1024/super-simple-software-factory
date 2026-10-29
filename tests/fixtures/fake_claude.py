#!/usr/bin/env python3
"""Stand-in for the `claude` CLI: replays a recorded stream-json run.

Point CLAUDE_PATH at this file for a zero-cost run of the real ADW path.
FAKE_CLAUDE_STREAM picks the stream (default: claude_stream.jsonl beside this
file); FAKE_CLAUDE_ARGV, when set, receives the argv and stdin as JSON lines so
a test can check how the adapter called it.
"""

import json
import os
import sys
from pathlib import Path

prompt = sys.stdin.read()
argv_log = os.environ.get("FAKE_CLAUDE_ARGV")
if argv_log:
    with open(argv_log, "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "stdin": prompt}) + "\n")

args = sys.argv[1:]
session = next((args[i + 1] for i, a in enumerate(args)
                if a in ("--session-id", "--resume") and i + 1 < len(args)), "unknown")
stream = Path(os.environ.get("FAKE_CLAUDE_STREAM")
              or Path(__file__).with_name("claude_stream.jsonl"))
for line in stream.read_text().splitlines():
    print(line.replace("SESSION", session), flush=True)
