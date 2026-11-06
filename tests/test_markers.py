"""Tests for .github/actions/collect/pytest/flake0_markers.py.

Each test runs an inner pytest in a subprocess, with the plugin loaded through
PYTEST_PLUGINS and PYTHONPATH, the way the collect action loads it on a runner.
"""

import json
import pathlib
import re

import pytest

pytest_plugins = ["pytester"]

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1] / ".github/actions/collect/pytest"
TESTS = """
import pytest

@pytest.fixture
def broken():
    raise RuntimeError("setup failed")

def test_pass():
    pass

def test_fail():
    assert False

def test_skip():
    pytest.skip("not today")

def test_setup_error(broken):
    pass
"""


@pytest.fixture
def run(pytester, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(PLUGIN_DIR))
    monkeypatch.setenv("PYTEST_PLUGINS", "flake0_markers")
    pytester.makepyfile(test_inner=TESTS)
    return pytester.runpytest_subprocess


@pytest.fixture
def markers(pytester, monkeypatch):
    path = pytester.path / "markers.ndjson"
    monkeypatch.setenv("FLAKE0_MARKERS", str(path))
    return path


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_each_test_gets_a_start_and_a_finish(run, markers):
    run()
    records = lines(markers)
    assert len(records) == 8
    for name in ("test_pass", "test_fail", "test_skip", "test_setup_error"):
        events = [r["event"] for r in records if r["nodeid"] == f"test_inner.py::{name}"]
        assert events == ["start", "finish"]


def test_outcomes(run, markers):
    run()
    outcomes = {r["nodeid"]: r["outcome"] for r in lines(markers) if r["event"] == "finish"}
    assert outcomes == {
        "test_inner.py::test_pass": "passed",
        "test_inner.py::test_fail": "failed",
        "test_inner.py::test_skip": "skipped",
        "test_inner.py::test_setup_error": "error",
    }


def test_failure_text(run, markers):
    run()
    finish = {r["nodeid"].split("::")[1]: r for r in lines(markers) if r["event"] == "finish"}
    assert "assert False" in finish["test_fail"]["failure"]
    assert "RuntimeError: setup failed" in finish["test_setup_error"]["failure"]
    assert "failure" not in finish["test_pass"]
    assert "failure" not in finish["test_skip"]


def test_failure_text_keeps_its_end(run, markers, pytester):
    pytester.makepyfile(test_inner='def test_long():\n    raise ValueError("a" * 10000 + " END")\n')
    run()
    failure = next(r["failure"] for r in lines(markers) if r["event"] == "finish")
    assert len(failure) == 4000
    assert "END" in failure[-100:]  # pytest ends with the location, "test_inner.py:2: ValueError"


def test_failed_teardown_is_an_error(run, markers, pytester):
    pytester.makepyfile(
        test_inner="""
        import pytest

        @pytest.fixture
        def broken():
            yield
            raise RuntimeError("teardown failed")

        def test_x(broken):
            pass
        """
    )
    run()
    assert lines(markers)[-1]["outcome"] == "error"


def test_retried_attempt_counts_as_failed(run, markers, pytester):
    # Simulates the outcome pytest-retry reports. pytest-rerunfailures reports "rerun".
    pytester.makeconftest(
        """
        import pytest

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_logreport(report):
            if report.when == "call":
                report.outcome = "retried"
        """
    )
    pytester.makepyfile(test_inner="def test_x(): pass")
    result = run()
    assert result.ret != pytest.ExitCode.INTERNAL_ERROR
    assert lines(markers)[-1]["outcome"] == "failed"


def test_duration_sums_the_phases(run, markers, pytester):
    pytester.makepyfile(
        test_inner="""
        import time
        import pytest

        @pytest.fixture
        def slow():
            time.sleep(0.1)
            yield
            time.sleep(0.1)

        def test_x(slow):
            time.sleep(0.1)
        """
    )
    run()
    assert lines(markers)[-1]["duration"] >= 0.3


def test_ts_format(run, markers):
    run()
    for record in lines(markers):
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", record["ts"])


def test_unset_path_writes_nothing(run, pytester, monkeypatch):
    monkeypatch.delenv("FLAKE0_MARKERS", raising=False)
    run().assert_outcomes(passed=1, failed=1, skipped=1, errors=1)
    assert not list(pytester.path.rglob("*.ndjson"))


def test_unwritable_path_does_not_break_the_run(run, pytester, monkeypatch):
    monkeypatch.setenv("FLAKE0_MARKERS", str(pytester.path / "missing/dir/markers.ndjson"))
    run().assert_outcomes(passed=1, failed=1, skipped=1, errors=1)
