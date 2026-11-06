"""flake0-collect: write one start and one finish line per test to $FLAKE0_MARKERS.

The collect action loads this through PYTEST_PLUGINS and PYTHONPATH. It does
nothing with FLAKE0_MARKERS unset, and a failed write never fails the test run.
"""

import json
import os
from datetime import datetime, timezone

PATH = os.environ.get("FLAKE0_MARKERS")
SEVERITY = ["passed", "skipped", "failed", "error"]
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
        test["outcome"] = max(test["outcome"], phase_outcome(report), key=SEVERITY.index)
        test["duration"] += report.duration


def pytest_runtest_logfinish(nodeid):
    test = tests.pop(nodeid, None)
    if test:
        write("finish", nodeid, outcome=test["outcome"], duration=round(test["duration"], 3))
