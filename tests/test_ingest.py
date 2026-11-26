"""Tests for .github/actions/ingest/scripts/ingest.py, with Data Prepper faked."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / ".github/actions/ingest/scripts/ingest.py"
_spec = importlib.util.spec_from_file_location("ingest", _SCRIPT)
ingest = importlib.util.module_from_spec(_spec)
sys.modules["ingest"] = ingest
_spec.loader.exec_module(ingest)

TAGS = {
    "run_id": "42", "run_attempt": "1", "repo": "canonical/x", "workflow": "ci", "job": "it",
    "sha": "abc", "ref": "main", "runner_name": "r1", "runner_os": "Linux", "image_os": "ubuntu24",
    "arch": "amd64",
}


class FakeDataPrepper:
    """Records batches and answers like Data Prepper."""

    def __init__(self, busy=0, status=408):
        self.batches, self.docs, self.busy, self.status = [], {}, busy, status

    def __call__(self, body):
        if self.busy:
            self.busy -= 1
            if self.status is None:
                raise ingest.Failure("cannot reach Data Prepper: connection reset")
            return self.status, "busy"
        events = json.loads(body)
        self.batches.append(events)
        for event in events:
            assert set(event) == {"index", "id", "doc"}
            self.docs[(event["index"], event["id"])] = event["doc"]
        return 200, "200 OK"

    def of(self, prefix):
        return [doc for (index, _), doc in self.docs.items() if index.startswith(prefix)]


def metric(name, fields, ts, tags=TAGS):
    return {"name": name, "tags": tags, "fields": fields, "timestamp": ts}


def write_bundle(root, logs=None, metrics=None, markers=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "env-start.json").write_text(json.dumps(
        {"ts": "2026-11-06T10:00:00.000Z", "kernel": "6.8.0", "snaps": [{"name": "lxd"}], "changes": None}))
    (root / "env-end.json").write_text(json.dumps(
        {"ts": "2026-11-06T10:30:00.000Z", "kernel": "6.8.0", "snaps": [], "changes": []}))
    if metrics is None:
        metrics = [[metric("cpu", {"usage_steal": 0}, 1793959200000)]]
    (root / "metrics.json").write_text("".join(json.dumps({"metrics": b}) + "\n" for b in metrics))
    for rel, text in (logs or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    if markers is not None:
        (root / "markers.ndjson").write_text("".join(
            m if isinstance(m, str) else json.dumps(m) + "\n" for m in markers))
    return root


def load(root, fake=None):
    fake = fake or FakeDataPrepper()
    assert ingest.main([str(root)], post=fake) == 0
    return fake


@pytest.mark.parametrize("line, ts", [
    ("2026-11-06T06:41:20,474002+00:00 audit: apparmor=\"DENIED\"", "2026-11-06T06:41:20.474Z"),
    ("2026-11-06T08:41:11.729199+02:00 host snapd[1]: hi", "2026-11-06T06:41:11.729Z"),
    ("2026-11-06T06:41:11.123456789Z stdout F hello", "2026-11-06T06:41:11.123Z"),
    ("machine-0: 2026-09-25 10:00:00.000 INFO juju.worker x", "2026-09-25T10:00:00.000Z"),
    ("unit-valkey-0: 2026-09-25 10:00:01.5 DEBUG x", "2026-09-25T10:00:01.500Z"),
    ("# flake0: connect root:lxd:testing at 2026-09-25T10:00:00.100Z", "2026-09-25T10:00:00.100Z"),
    ("# flake0: reconnect 1 at 2026-09-25T10:05:00.100Z after exit 1", "2026-09-25T10:05:00.100Z"),
    ("2026-11-06T06:41:11Z I! Starting Telegraf", "2026-11-06T06:41:11.000Z"),
    ("2026-11-06T06:41:20.1234567Z ##[group]Run actions/checkout", "2026-11-06T06:41:20.123Z"),
    ("[2026-11-06T06:41:11,123][INFO ][o.o.n.Node] started", "2026-11-06T06:41:11.123Z"),
    ("[2026-11-06 06:41:11,123] INFO kafka", "2026-11-06T06:41:11.123Z"),
    ("2026-11-06 06:41:11.123 UTC [12] LOG: ready", "2026-11-06T06:41:11.123Z"),
    ('{"t":{"$date":"2026-11-06T06:41:11.123+00:00"},"s":"I"}', "2026-11-06T06:41:11.123Z"),
    ("1:M 06 Nov 2026 06:41:11.123 * Ready to accept connections", "2026-11-06T06:41:11.123Z"),
    ("2026-11-06T06:41:11-0500 x", "2026-11-06T11:41:11.000Z"),
])
def test_parse_ts(line, ts):
    assert ingest.parse_ts(line) == ts


@pytest.mark.parametrize("line", [
    "Traceback (most recent call last):",
    "tests/test_x.py::test_y PASSED",
    "2026-13-45T99:00:00Z not a date",
    "",
])
def test_parse_ts_none(line):
    assert ingest.parse_ts(line) is None


def test_metrics_shape_and_numbers(tmp_path):
    fake = load(write_bundle(tmp_path / "b", metrics=[[
        metric("cpu", {"usage_steal": 0}, 1793959200000),
        metric("cpu", {"usage_steal": 0.7}, 1793959202000),
    ]]))
    docs = sorted(fake.of("ci-metrics-2026.11"), key=lambda d: d["@timestamp"])
    assert docs[0] == {
        "@timestamp": "2026-11-06T10:00:00.000Z", "measurement_name": "cpu", "tag": TAGS,
        "cpu": {"usage_steal": 0}, "bundle_id": docs[0]["bundle_id"],
    }
    assert docs[1]["cpu"] == {"usage_steal": 0.7}


def test_collector_metrics_kept_apart(tmp_path):
    fake = load(write_bundle(tmp_path / "b", metrics=[[
        metric("cpu", {"usage_steal": 0}, 1793959200000),
        metric("internal_gather", {"gather_time_ns": 5}, 1793959200000),
    ]]))
    assert [d["measurement_name"] for d in fake.of("ci-metrics-")] == ["cpu"]
    assert [d["measurement_name"] for d in fake.of("ci-flake0-metrics-2026.11")] == ["internal_gather"]


def test_metrics_with_the_same_series_key_both_kept(tmp_path):
    # pressure emits {total} and {avg10,...} as two metrics with one time and tag set
    tags = {**TAGS, "resource": "cpu", "type": "some"}
    fake = load(write_bundle(tmp_path / "b", metrics=[[
        metric("pressure", {"total": 5}, 1793959200000, tags),
        metric("pressure", {"avg10": 0.5}, 1793959200000, tags),
    ]]))
    assert len(fake.of("ci-metrics-")) == 2


def test_rotated_metrics_and_bad_lines(tmp_path, capsys):
    root = write_bundle(tmp_path / "b")
    (root / "metrics.2026-11-06-1793947266.json").write_text(
        json.dumps({"metrics": [metric("mem", {"used": 1}, 1793959200000)]}) + "\n" + '{"metrics": [{"na')
    fake = load(root)
    assert {d["measurement_name"] for d in fake.of("ci-metrics-")} == {"cpu", "mem"}
    assert "skipped 1 unreadable line" in capsys.readouterr().err


def test_reload_overwrites_and_does_not_duplicate(tmp_path):
    root = write_bundle(tmp_path / "b", logs={"kernel.log": "2026-11-06T10:00:01,000000+00:00 a\n"},
                        markers=[{"ts": "2026-11-06T10:00:00.000Z", "event": "start", "nodeid": "t"}])
    fake = load(root)
    first = len(fake.docs)
    load(root, fake)
    assert len(fake.docs) == first


def test_log_docs(tmp_path):
    fake = load(write_bundle(tmp_path / "b", logs={
        "kernel.log": "2026-11-06T10:00:01,000000+00:00 Out of memory\n\n continuation\n",
        "pytest.log": "collected 2 items\nFAILED test_x\n",
    }))
    kernel = sorted((d for d in fake.of("ci-logs-") if d["file"] == "kernel.log"), key=lambda d: d["line"])
    assert [(d["line"], d["message"], d["@timestamp"]) for d in kernel] == [
        (1, "Out of memory", "2026-11-06T10:00:01.000Z"),
        (3, " continuation", "2026-11-06T10:00:01.000Z"),
    ]
    assert kernel[0]["tag"] == TAGS
    pytest_docs = [d for d in fake.of("ci-logs-pytest-2026.11")]
    assert len(pytest_docs) == 2
    assert all("@timestamp" not in d for d in pytest_docs)


def test_cri_prefix_stripped(tmp_path):
    fake = load(write_bundle(tmp_path / "b", logs={"workload/k8s/valkey-0_testing_charm-1.log": (
        "# flake0: connect k8s:valkey-0 at 2026-11-06T10:00:00.000Z\n"
        "2026-11-06T10:00:01.123456789Z stdout F hello world\n"
        "2026-11-06T10:00:02.000000000Z stderr P \n"
    )}))
    messages = [d["message"] for d in sorted(fake.of("ci-logs-"), key=lambda d: d["line"])]
    assert messages == ["# flake0: connect k8s:valkey-0 at 2026-11-06T10:00:00.000Z", "hello world", ""]


def fields(fake, rel):
    """The parsed fields and message of each line of `rel`, in order."""
    shared = {"bundle_id", "tag", "file", "line", "@timestamp"}
    docs = sorted((d for d in fake.of("ci-logs-") if d["file"] == rel), key=lambda d: d["line"])
    return [{k: v for k, v in d.items() if k not in shared} for d in docs]


def test_juju_fields(tmp_path):
    rel = "juju-debug-root-concierge-lxd-testing.log"
    fake = load(write_bundle(tmp_path / "b", logs={rel: (
        "# flake0: connect root:concierge-lxd:testing at 2026-11-06T10:00:00.000Z\n"
        "unit-valkey-0: 2026-11-06 10:00:01.000 ERROR unit.valkey/0.juju-log boom\n"
        "Traceback (most recent call last):\n"
        "# flake0: reconnect 1 at 2026-11-06T10:05:00.000Z after exit 1\n"
    )}))
    where = {"controller": "concierge-lxd", "model": "testing"}
    error = {**where, "entity": "unit-valkey-0", "level": "ERROR", "module": "unit.valkey/0.juju-log"}
    assert fields(fake, rel) == [
        {**where, "message": "# flake0: connect root:concierge-lxd:testing at 2026-11-06T10:00:00.000Z"},
        {**error, "message": "boom"},
        {**error, "message": "Traceback (most recent call last):"},  # takes the fields above
        {**where, "message": "# flake0: reconnect 1 at 2026-11-06T10:05:00.000Z after exit 1"},
    ]


def test_k8s_fields(tmp_path):
    rel = "workload/k8s/opensearch-0_testing_charm-" + "a" * 64 + ".log"
    fake = load(write_bundle(tmp_path / "b", logs={rel: (
        "2026-11-06T07:31:52.929628643Z stdout F 2026-11-06T07:31:52.929Z [container-agent] starting unit\n"
        "2026-11-06T07:31:53.000000000Z stderr F level=info msg=plain\n"
    )}))
    pod = {"pod": "opensearch-0", "namespace": "testing", "container": "charm"}
    assert fields(fake, rel) == [
        {**pod, "stream": "stdout", "service": "container-agent", "message": "starting unit"},
        {**pod, "stream": "stderr", "message": "level=info msg=plain"},
    ]


def test_journal_fields(tmp_path):
    lxd, snapd = "workload/lxd/juju-366b0b-0/journal.log", "snapd.log"
    fake = load(write_bundle(tmp_path / "b", logs={
        lxd: ("2026-11-06T06:41:11.729199+00:00 juju-366b0b-0 systemd[1]: Started x.\n"
              "2026-11-06T06:41:12.000000+00:00 juju-366b0b-0 kernel: hello\n"
              "-- Boot 1234 --\n"),
        snapd: "2026-11-06T06:41:13.000000+00:00 runner snapd[812]: api.go:12: refresh\n",
    }))
    container = {"container": "juju-366b0b-0"}
    assert fields(fake, lxd) == [
        {**container, "host": "juju-366b0b-0", "process": "systemd", "pid": "1", "message": "Started x."},
        {**container, "host": "juju-366b0b-0", "process": "kernel", "message": "hello"},
        {**container, "host": "juju-366b0b-0", "process": "kernel", "message": "-- Boot 1234 --"},
    ]
    assert fields(fake, snapd) == [{"host": "runner", "process": "snapd", "pid": "812", "message": "api.go:12: refresh"}]


def test_app_fields(tmp_path):
    rel = "workload/lxd/juju-366b0b-0/valkey-charmed_valkey_topology_observer.log"
    line = "2026-11-06 06:46:02 INFO     Starting new observer"
    fake = load(write_bundle(tmp_path / "b", logs={rel: line + "\n"}))
    assert fields(fake, rel) == [{"container": "juju-366b0b-0", "snap": "valkey-charmed",
                                  "log": "valkey_topology_observer.log", "message": line}]


def test_job_steps(tmp_path):
    fake = load(write_bundle(tmp_path / "b", logs={"job.log": (
        "2026-11-06T06:41:20.1000000Z Current runner version: '2.338.2'\n"
        "2026-11-06T06:41:21.1000000Z ##[group]Run actions/checkout@v4\n"
        "2026-11-06T06:41:22.1000000Z Syncing repository\n"
    )}))
    assert fields(fake, "job.log") == [
        {"message": "Current runner version: '2.338.2'"},
        {"step": "actions/checkout@v4", "message": "##[group]Run actions/checkout@v4"},
        {"step": "actions/checkout@v4", "message": "Syncing repository"},
    ]


def test_skip_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("INGEST_SKIP_STEPS", "juju debug-log\n jhack tail \n\n")
    lines = ["##[group]Run sudo juju debug-log --replay", "unit-v-0: started",
             "##[group]Run sudo jhack tail", "hook", "##[group]Run x", "y"]
    fake = load(write_bundle(tmp_path / "b", logs={"job.log": "".join(
        f"2026-11-06T06:41:2{i}.1000000Z {line}\n" for i, line in enumerate(lines))}))
    assert [d["message"] for d in fake.of("ci-logs-job-")] == ["##[group]Run x", "y"]


def test_skip_files(tmp_path, monkeypatch):
    monkeypatch.setenv("INGEST_SKIP_FILES", "workload/k8s/controller-*_api-server-*.log")
    line = "2026-11-06T07:31:52.929628643Z stdout F hi\n"
    fake = load(write_bundle(tmp_path / "b", logs={
        "workload/k8s/controller-0_controller-k8s_api-server-" + "a" * 64 + ".log": line,
        "workload/k8s/v-0_testing_charm-" + "b" * 64 + ".log": line,
    }))
    assert [d["container"] for d in fake.of("ci-logs-k8s-")] == ["charm"]


def test_replays_collapse_and_tracebacks_stay(tmp_path):
    history = (
        "unit-v-0: 2026-11-06 10:00:01.000 ERROR boom\n"
        "Traceback (most recent call last):\n"
        "unit-v-0: 2026-11-06 10:00:02.000 ERROR boom again\n"
        "Traceback (most recent call last):\n"
    )
    fake = load(write_bundle(tmp_path / "b", logs={"juju-debug-root-lxd-testing.log": (
        "# flake0: connect root:lxd:testing at 2026-11-06T10:00:00.000Z\n" + history
        + "# flake0: reconnect 1 at 2026-11-06T10:05:00.000Z after exit 1\n" + history
    )}))
    logs = fake.of("ci-logs-")
    # 2 markers + 4 history lines, each traceback line once per error
    assert len(logs) == 6
    assert sum(d["message"].startswith("Traceback") for d in logs) == 2


def test_repeated_lines_without_a_reconnect_kept(tmp_path):
    # cloud-init prints identical table borders in one tick
    border = "2026-11-06T10:00:01.000000+00:00 host cloud-init[1]: ci-info: +--------+\n"
    fake = load(write_bundle(tmp_path / "b", logs={"workload/lxd/c/journal.log": (
        "# flake0: connect lxd:c at 2026-11-06T10:00:00.000Z\n" + border * 3
    )}))
    assert len(fake.of("ci-logs-")) == 4


def test_badly_shaped_metrics_skipped(tmp_path, capsys):
    fake = load(write_bundle(tmp_path / "b", metrics=[
        [metric("cpu", {"usage_steal": 0}, 1793959200000)],
        [{"name": "cpu", "fields": {}, "tags": {}}],
        ["x"],
        [{"name": "cpu", "fields": {}, "tags": {}, "timestamp": 10**20}],
    ]))
    assert len(fake.of("ci-metrics-")) == 1
    assert "skipped 3 badly shaped lines in metrics.json" in capsys.readouterr().err


def test_symlinks_and_other_files_skipped(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("2026-11-06T10:00:00Z private key\n")
    root = write_bundle(tmp_path / "b", logs={"telegraf.resolved.conf": "[agent]\n"})
    (root / "kernel.log").symlink_to(secret)
    fake = load(root)
    assert fake.of("ci-logs-") == []
    assert all("private" not in json.dumps(d) for d in fake.docs.values())


def test_meta_docs(tmp_path):
    fake = load(write_bundle(tmp_path / "b", markers=[
        {"ts": "2026-11-06T10:00:00.000Z", "event": "start", "nodeid": "t.py::flaky"},
        {"ts": "2026-11-06T10:00:05.000Z", "event": "finish", "nodeid": "t.py::flaky", "outcome": "failed", "duration": 5.0,
         "failure": "E   assert 1 == 2"},
        {"ts": "2026-11-06T10:00:06.000Z", "event": "start", "nodeid": "t.py::flaky"},
        {"ts": "2026-11-06T10:00:07.000Z", "event": "finish", "nodeid": "t.py::flaky", "outcome": "passed", "duration": 1.0},
        '{"ts": "2026-11-06T10:00:0\n',
        {"ts": None, "event": "start", "nodeid": "t.py::broken"},
        {"ts": "2026-11-06T10:00:08.000Z", "event": "start", "nodeid": "t.py::hung"},
    ]))
    [run] = fake.of("ci-meta")
    tests = fake.of("ci-tests")
    assert run["@timestamp"] == "2026-11-06T10:00:00.000Z"
    assert run["end"] == "2026-11-06T10:30:00.000Z"
    assert run["env_start"]["snaps"] == [{"name": "lxd"}]
    assert run["tag"] == TAGS
    assert [d.get("failure") for d in tests if d["outcome"] == "failed"] == ["E   assert 1 == 2"]
    assert {d["name"] for d in tests} == {"flaky", "hung"}
    assert sorted((d["nodeid"], d["attempt"], d["outcome"], d["@timestamp"], d["end"]) for d in tests) == [
        ("t.py::flaky", 1, "failed", "2026-11-06T10:00:00.000Z", "2026-11-06T10:00:05.000Z"),
        ("t.py::flaky", 2, "passed", "2026-11-06T10:00:06.000Z", "2026-11-06T10:00:07.000Z"),
        ("t.py::hung", 1, "unfinished", "2026-11-06T10:00:08.000Z", "2026-11-06T10:30:00.000Z"),
    ]


def test_docs_labelled_with_the_running_test(tmp_path):
    logs = {"kernel.log": "2026-11-06T10:00:02Z a\n2026-11-06T10:00:05.500Z b\n2026-11-06T10:00:06.500Z c\n",
            "job.log": "2026-11-06T10:00:02.0000000Z d\n"}
    fake = load(write_bundle(tmp_path / "b", logs=logs, markers=[
        {"ts": "2026-11-06T10:00:00.000Z", "event": "start", "nodeid": "t.py::a"},
        {"ts": "2026-11-06T10:00:05.000Z", "event": "finish", "nodeid": "t.py::a", "outcome": "failed", "duration": 5.0},
        {"ts": "2026-11-06T10:00:06.000Z", "event": "start", "nodeid": "t.py::b"},
        {"ts": "2026-11-06T10:00:07.000Z", "event": "finish", "nodeid": "t.py::b", "outcome": "passed", "duration": 1.0},
    ]))
    assert {d["message"]: d.get("test", {}).get("name") for d in fake.of("ci-logs-host")} == {"a": "a", "b": None, "c": "b"}
    assert fake.of("ci-metrics")[0]["test"] == {"nodeid": "t.py::a", "name": "a", "attempt": 1, "outcome": "failed"}
    assert all("test" not in d for d in fake.of("ci-logs-job") + fake.of("ci-tests") + fake.of("ci-meta"))


def test_job_identity_on_every_doc(tmp_path):
    job_log = "2026-11-06T10:00:00.0000000Z Complete job name: it (lxd)\n"
    fake = load(write_bundle(tmp_path / "b", logs={"job.log": job_log}))
    assert {d["tag"]["job_name"] for d in fake.docs.values()} == {"it (lxd)"}


def juju_status(ts, workload, message):
    unit = {"workload-status": {"current": workload, "message": message}, "juju-status": {"current": "idle"},
            "subordinates": {"grafana-agent/0": {"workload-status": {"current": "active"},
                                                 "juju-status": {"current": "executing"}}}}
    status = {"model": {"name": "testing", "controller": "concierge-lxd"},
              "applications": {"valkey": {"units": {"valkey/0": unit}}, "empty": {}}}
    return json.dumps({"ts": ts, "status": status}) + "\n"


def test_verdict_doc(tmp_path, monkeypatch):
    verdict = tmp_path / "verdict.json"
    verdict.write_text(json.dumps({"job": "it (lxd)", "model": "m", "verdict": "FLAKY", "confidence_pct": 80,
                                   "failed_tests": ["test_x"], "bundle_id": "spoofed"}))
    monkeypatch.setenv("INGEST_VERDICT_PATH", str(verdict))
    fake = load(write_bundle(tmp_path / "b"))
    load(write_bundle(tmp_path / "b"), fake)
    [doc] = fake.of("ci-verdicts")
    run = next(d for d in fake.of("ci-meta") if d["type"] == "run")
    assert doc["bundle_id"] == run["bundle_id"] and doc["tag"] == TAGS
    assert doc["@timestamp"] == "2026-11-06T10:00:00.000Z"
    assert doc["verdict"] == "FLAKY"


@pytest.mark.parametrize("text", [None, "{not json", "[]"])
def test_bad_verdict_skipped(tmp_path, monkeypatch, capsys, text):
    verdict = tmp_path / "verdict.json"
    if text is not None:
        verdict.write_text(text)
    monkeypatch.setenv("INGEST_VERDICT_PATH", str(verdict))
    fake = load(write_bundle(tmp_path / "b"))
    assert fake.of("ci-verdicts") == [] and fake.of("ci-meta")
    assert "verdict" in capsys.readouterr().err


def test_juju_status_docs(tmp_path, capsys):
    fake = load(write_bundle(tmp_path / "b", logs={"juju-status-root-concierge-lxd-testing.ndjson": (
        juju_status("2026-11-06T10:00:00.000Z", "maintenance", "installing")
        + juju_status("2026-11-06T10:00:30.000Z", "error", 'hook failed: "start"')
        + '{"ts": "2026-11-06T10:01:00.000Z", "status": "x"}\n'
    )}))
    docs = sorted(fake.of("ci-juju-status-2026.11"), key=lambda d: d["@timestamp"])
    assert all(d["type"] != "juju_status" for d in fake.of("ci-meta"))
    assert "type" not in docs[0]
    assert [d["@timestamp"] for d in docs] == ["2026-11-06T10:00:00.000Z", "2026-11-06T10:00:30.000Z"]
    assert (docs[1]["controller"], docs[1]["model"], docs[1]["tag"]) == ("concierge-lxd", "testing", TAGS)
    assert docs[1]["units"] == [
        {"app": "valkey", "unit": "valkey/0", "workload_status": "error", "agent_status": "idle",
         "message": 'hook failed: "start"'},
        {"app": "grafana-agent", "unit": "grafana-agent/0", "workload_status": "active",
         "agent_status": "executing", "message": None},
    ]
    assert docs[1]["status"]["applications"]["valkey"]["units"]["valkey/0"]["workload-status"]["current"] == "error"
    assert "skipped 1 badly shaped line in juju-status-root-concierge-lxd-testing.ndjson" in capsys.readouterr().err


def test_bundle_id_tells_matrix_legs_apart(tmp_path):
    fake = FakeDataPrepper()
    load(write_bundle(tmp_path / "a"), fake)
    load(write_bundle(tmp_path / "b", metrics=[[metric("cpu", {"x": 1}, 1793959200000, {**TAGS, "runner_name": "r2"})]]), fake)
    assert len({d["bundle_id"] for d in fake.of("ci-meta")}) == 2


def test_tags_fall_back_to_env_without_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "99")
    monkeypatch.delenv("GITHUB_JOB", raising=False)
    root = write_bundle(tmp_path / "b", metrics=[], logs={"kernel.log": "2026-11-06T10:00:01Z x\n"})
    fake = load(root)
    tag = fake.of("ci-logs-")[0]["tag"]
    assert tag["run_id"] == "99"
    assert tag["job"] == "local"


def test_archive_input(tmp_path):
    root = write_bundle(tmp_path / "b", logs={"kernel.log": "2026-11-06T10:00:01Z x\n"})
    archive = tmp_path / "flake0-bundle.tar.gz"
    subprocess.run(["tar", "-czf", archive, "-C", root, "."], check=True)
    fake = load(archive)
    assert [d["file"] for d in fake.of("ci-logs-")] == ["kernel.log"]


def test_logs_split_by_source(tmp_path):
    sources = {
        "juju-debug-root-lxd-testing.log": "juju", "workload/k8s/valkey-0_testing_charm-1.log": "k8s",
        "workload/lxd/c1/journal.log": "lxd", "workload/lxd/c1/valkey-charmed_valkey_valkey.log": "app",
        "kernel.log": "host", "snapd.log": "host", "job.log": "job", "pytest.log": "pytest",
        "telegraf.log": None, "watcher.log": None, "workload-watcher.log": None,
    }
    fake = load(write_bundle(tmp_path / "b", logs={rel: "2026-11-06T10:00:01Z x\n" for rel in sources}))
    indices = {doc["file"]: index for (index, _), doc in fake.docs.items() if "logs-" in index}
    # None: the collector's own logs, kept out of ci-logs-*
    assert indices == {rel: f"ci-logs-{source}-2026.11" if source else "ci-flake0-logs-2026.11"
                       for rel, source in sources.items()}


@pytest.mark.parametrize("status", [408, None])
def test_throttled_bulk_retried(tmp_path, monkeypatch, status):
    monkeypatch.setattr(ingest.time, "sleep", lambda _: None)
    fake = load(write_bundle(tmp_path / "b"), FakeDataPrepper(busy=2, status=status))
    assert fake.of("ci-metrics-")


def test_batches_split(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "BATCH_BYTES", 200)
    fake = load(write_bundle(tmp_path / "b", logs={"kernel.log": "".join(
        f"2026-11-06T10:00:{i:02d}Z line {i}\n" for i in range(10))}))
    assert len(fake.batches) > 1
    assert len(fake.of("ci-logs-")) == 10


@pytest.mark.parametrize("env, error", [
    ({}, "https://"),
    ({"INGEST_URL": "http://example.com/log/ingest"}, "https://"),
    ({"INGEST_URL": "https://example.com/log/ingest", "INGEST_CA": "/nonexistent"}, "/nonexistent"),
])
def test_connect_refuses_bad_settings(env, error):
    with pytest.raises(ingest.Failure, match=error):
        ingest.connect(env)


def test_unreachable_cluster_fails(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ingest.time, "sleep", lambda _: None)
    post = ingest.connect({"INGEST_URL": "https://127.0.0.1:1/log/ingest"})
    assert ingest.main([str(write_bundle(tmp_path / "b"))], post=post) == 1
    err = capsys.readouterr().err
    assert "cannot reach Data Prepper" in err and "127.0.0.1" not in err


def test_missing_bundle_fails(tmp_path, capsys):
    assert ingest.main([str(tmp_path / "nope.tar.zst")], post=FakeDataPrepper()) == 1
    assert "nope.tar.zst" in capsys.readouterr().err
