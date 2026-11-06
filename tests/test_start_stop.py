"""Tests for the Phase 3 parts of start.sh and stop.sh, with tests/fakes/telegraf."""

import json
import os
import pathlib
import shutil
import subprocess
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
ACTION = REPO / ".github/actions/collect"


def run_stop(collect, runner_temp, **env):
    subprocess.run(
        [ACTION / "scripts/stop.sh"],
        env={
            **os.environ,
            "FLAKE0_COLLECT_DIR": str(collect),
            "RUNNER_TEMP": str(runner_temp),
            "BUNDLE_PATH": str(runner_temp / "bundle.tar.zst"),
            **env,
        },
        check=True,
        timeout=60,
    )


@pytest.fixture
def start(tmp_path):
    """Runs start.sh with the fake telegraf. Returns the lines it put in $GITHUB_ENV."""
    collect = tmp_path / "collect"
    github_env = tmp_path / "github_env"
    github_env.touch()
    home = tmp_path / "telegraf"
    (home / "telegraf-1.39.2/usr/bin").mkdir(parents=True)
    shutil.copy(REPO / "tests/fakes/telegraf", home / "telegraf-1.39.2/usr/bin/telegraf")

    def run(**env):
        subprocess.run(
            [ACTION / "scripts/start.sh"],
            env={
                **os.environ,
                "FLAKE0_COLLECT_DIR": str(collect),
                "FLAKE0_LOGS": "false",
                "GITHUB_ENV": str(github_env),
                "RUNNER_TEMP": str(tmp_path),
                "TELEGRAF_HOME": str(home),
                **env,
            },
            check=True,
            timeout=60,
        )
        return github_env.read_text().splitlines()

    yield run
    run_stop(collect, tmp_path)


def test_pytest_is_left_alone_by_default(start):
    assert start() == []


def test_markers_input_adds_to_the_jobs_values(start, tmp_path):
    lines = start(FLAKE0_LOAD_MARKERS="true", PYTHONPATH="/prior", PYTEST_PLUGINS="other")
    assert lines == [
        f"FLAKE0_MARKERS={tmp_path}/collect/markers.ndjson",
        "PYTEST_PLUGINS=other,flake0_markers",
        f"PYTHONPATH={ACTION}/pytest:/prior",
    ]


def test_test_log_is_redacted(tmp_path):
    collect = tmp_path / "collect"
    collect.mkdir()
    log = tmp_path / "test.log"
    log.write_text("password=hunter2\n")
    run_stop(collect, tmp_path, FLAKE0_TEST_LOG=str(log))
    assert "hunter2" not in (collect / "pytest.log").read_text()


def test_nodeids_in_markers_are_redacted(tmp_path):
    collect = tmp_path / "collect"
    collect.mkdir()
    (collect / "markers.ndjson").write_text('{"event":"start","nodeid":"a.py::test_p[password=hunter2]"}\n')
    run_stop(collect, tmp_path)
    assert "hunter2" not in json.loads((collect / "markers.ndjson").read_text())["nodeid"]


def test_stop_removes_the_plugins_bytecode(tmp_path):
    # Root's pytest writes it inside spread. A reused runner could not delete the action.
    cache = ACTION / "pytest/__pycache__"
    cache.mkdir(exist_ok=True)
    collect = tmp_path / "collect"
    collect.mkdir()
    run_stop(collect, tmp_path)
    assert not cache.exists()


def test_job_log_keeps_pages_the_runner_deleted(tmp_path):
    job, other = "1a2b-3c", "9f9f-00"
    diag = tmp_path / "_diag"
    (diag / "pages").mkdir(parents=True)
    (diag / "Worker_20261106-100000-utc.log").write_text(f"[2026-11-06 10:00:00Z INFO JobRunner] Job ID {other}\n")
    (diag / "Worker_20261106-110000-utc.log").write_text(f"[2026-11-06 11:00:00Z INFO JobRunner] Job ID {job}\n")
    pages = {n: diag / f"pages/plan_{job}_{n}.log" for n in (1, 2, 10)}
    for n, page in pages.items():
        page.write_text(f"﻿page {n}\n", encoding="utf-8")
    (diag / f"pages/plan_{other}_3.log").write_text("other job\n")
    collect = tmp_path / "collect"
    collect.mkdir()
    subprocess.Popen(
        [ACTION / "scripts/joblog-watch.sh", collect],
        env={**os.environ, "FLAKE0_RUNNER_DIAG": str(diag)},
        start_new_session=True,
    )
    kept = collect / "joblog-pages"
    deadline = time.monotonic() + 10
    while len(list(kept.glob("*.log"))) < 3 and time.monotonic() < deadline:
        time.sleep(0.1)
    for page in pages.values():
        page.unlink()  # as the runner does after the upload
    run_stop(collect, tmp_path)
    assert (collect / "job.log").read_text(encoding="utf-8") == "page 1\npage 2\npage 10\n"
    assert not kept.exists()
    assert not (collect / "joblog-watcher.pid").exists()
