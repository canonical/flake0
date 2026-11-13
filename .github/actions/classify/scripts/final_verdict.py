#!/usr/bin/env python3
"""Print the last JSON object with a "verdict" key in the agent's messages.

    final_verdict.py <session.ndjson>

At the steps cap opencode turns tools off and the agent answers in text, so
the verdict it could not write is in its last message. Exits 1 when there is
none.
"""

import json
import sys

texts = []
with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
    for line in fh:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "text":
            texts.append(str((event.get("part") or {}).get("text") or ""))

decoder = json.JSONDecoder()
for text in reversed(texts):
    found = None
    for i, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text, i)
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict) and "verdict" in value:
            found = value
    if found is not None:
        print(json.dumps(found))
        sys.exit(0)
sys.exit(1)
