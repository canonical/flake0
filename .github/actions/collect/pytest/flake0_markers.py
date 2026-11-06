"""flake0-collect: write one start and one finish line per test to $FLAKE0_MARKERS.

The collect action loads this through PYTEST_PLUGINS and PYTHONPATH. It does
nothing with FLAKE0_MARKERS unset, and a failed write never fails the test run.
"""

import json
import os
import re
from datetime import datetime, timezone

PATH = os.environ.get("FLAKE0_MARKERS")
SEVERITY = ["passed", "skipped", "failed", "error"]
FAILURE_CHARS = 4000  # the end of a traceback, where the exception and the assertion are
# redact.sed's private-key pattern. Masked here, because sed's multi-line rule masks from a key
# header with no footer to the end of markers.ndjson, and a JSON line has no footer line.
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----.*?(?:-----END (?:[A-Z]+ )?PRIVATE KEY-----|\Z)", re.DOTALL)
# shortcut: under pytest-xdist the controller and each worker write, so lines repeat. No-op on PYTEST_XDIST_WORKER if a consumer adopts it.
tests = {}


def write(event, nodeid, **fields):
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    line = json.dumps({"ts": now, "event": event, "nodeid": nodeid, **fields}, separators=(",", ":"))
    try:
        # One write per line on an O_APPEND file, and a fresh open, so a test that
        # kills pytest keeps every earlier line.
        fd = os.open(PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, f"{line}\n".encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def phase_outcome(report):
    if report.outcome in ("passed", "skipped"):
        return report.outcome
    # Also covers retry plugins, which report their own outcome for a failed attempt.
    return "failed" if report.when == "call" else "error"


def pytest_runtest_logstart(nodeid):
    if PATH:
        tests[nodeid] = {"outcome": "passed", "duration": 0.0}
        write("start", nodeid)


def pytest_runtest_logreport(report):
    test = tests.get(report.nodeid)
    if test:
        outcome = phase_outcome(report)
        test["outcome"] = max(test["outcome"], outcome, key=SEVERITY.index)
        test["duration"] += report.duration
        # The first failing phase is the cause: a teardown error after a failed call adds less.
        if outcome in ("failed", "error") and "failure" not in test:
            # Masked before the cut, which could drop a header and keep the key.
            test["failure"] = PRIVATE_KEY.sub("***REDACTED-PRIVATE-KEY-BLOCK***", report.longreprtext)[-FAILURE_CHARS:]


def pytest_runtest_logfinish(nodeid):
    test = tests.pop(nodeid, None)
    if test:
        write("finish", nodeid, **{**test, "duration": round(test["duration"], 3)})
