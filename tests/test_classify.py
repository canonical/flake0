import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "actions"
    / "classify"
    / "scripts"
    / "classify.py"
)
_spec = importlib.util.spec_from_file_location("classify", _SCRIPT_PATH)
classify = importlib.util.module_from_spec(_spec)
sys.modules["classify"] = classify
_spec.loader.exec_module(classify)


def _metric(name, tags, fields, ts):
    return {"name": name, "tags": tags, "fields": fields, "timestamp": ts}


def _bundle(path, metrics, runner="GitHub Actions 1", attempt="1"):
    path.mkdir()
    for m in metrics:
        m["tags"].update(runner_name=runner, run_attempt=attempt)
    (path / "metrics.json").write_text(json.dumps({"metrics": metrics}) + "\n")
    return path


def _main_args(tmp_path, jobs):
    (tmp_path / "jobs.ndjson").write_text("".join(json.dumps(j) + "\n" for j in jobs))
    (tmp_path / "logs").mkdir()
    (tmp_path / "bundles").mkdir()
    args = ["--jobs", str(tmp_path / "jobs.ndjson"), "--logs", str(tmp_path / "logs")]
    return args + ["--bundles", str(tmp_path / "bundles"), "--out", str(tmp_path / "out"), "--model", "m"]


def test_clip_keeps_head_and_tail():
    text = "a" * 50 + "b" * 50
    out = classify.clip(text, 20)
    assert out.startswith("a" * 10)
    assert out.endswith("b" * 10)
    assert "80 characters cut" in out
    assert classify.clip("short", 20) == "short"


def test_excerpt_keeps_the_last_matches_before_the_tail(tmp_path):
    log = tmp_path / "job.log"
    # setup noise matches too, and must not push the test failure out
    lines = ["Error: apt noise"] * 150 + ["\x1b[31mFAILED test_x\x1b[0m"] + ["dump"] * 400
    lines[0] = "2026-10-02T13:42:32.2534341Z Error: apt noise"
    log.write_text("\n".join(lines))
    out = classify.excerpt(log, tail=300, max_hits=10)
    assert "FAILED test_x" in out
    assert "\x1b" not in out
    assert "last 10 of 151 matching lines" in out
    short = classify.excerpt(log, tail=1000)
    assert short.splitlines()[1] == "13:42:32 Error: apt noise"


def test_summarize_metrics_picks_the_right_series(tmp_path):
    bundle = _bundle(
        tmp_path / "b",
        [
            _metric("pressure", {"resource": "cpu", "type": "some"}, {"avg10": 5.0}, 1_000),
            _metric("pressure", {"resource": "cpu", "type": "some"}, {"avg10": 80.0}, 3_000),
            # a hostile or odd value is skipped, not summed
            _metric("pressure", {"resource": "cpu", "type": "some"}, {"avg10": "x"}, 3_500),
            # another resource must not leak into the cpu line
            _metric("pressure", {"resource": "io", "type": "some"}, {"avg10": 99.0}, 2_000),
            _metric("disk", {"path": "/"}, {"used_percent": 97.5, "inodes_free": 12}, 2_000),
            _metric("nstat", {}, {"TcpRetransSegs": 10}, 1_000),
            _metric("nstat", {}, {"TcpRetransSegs": 25}, 3_000),
        ],
    )
    summary = classify.summarize_metrics(classify.load_metrics(bundle))
    assert "PSI cpu some avg10 %: min 5.0, mean 42.5, p95 80.0, max 80.0 at 00:00:03Z (2 samples)" in summary
    assert "PSI io some avg10 %: min 99.0" in summary
    assert "Disk / inodes free: min 12" in summary
    assert "TCP retransmitted segments during the run: 15" in summary


def test_load_metrics_skips_malformed_records(tmp_path):
    (tmp_path / "metrics.json").write_text(
        '{"metrics": [{"name": "mem", "tags": [], "fields": {}, "timestamp": 1}]}\n'
        '{"metrics": null}\n[1]\n{"trunc'
    )
    (tmp_path / "metrics2.json").mkdir()  # unreadable as a file
    assert classify.load_metrics(tmp_path) == {}


def test_unit_timeline_lists_each_status_change(tmp_path):
    def snap(ts, workload, msg=""):
        unit = {"workload-status": {"current": workload, "message": msg}, "juju-status": {"current": "idle"}}
        return json.dumps({"ts": ts, "status": {"applications": {"valkey": {"units": {"valkey/0": unit}}}}})

    status = tmp_path / "juju-status-root-c-m.ndjson"
    lines = [snap("t1", "maintenance"), snap("t2", "maintenance"), snap("t3", "error", "hook failed")]
    router = {"workload-status": {"current": "error"}, "juju-status": {"current": "idle"}}
    principal = {"workload-status": {"current": "error", "message": "hook failed"}, "juju-status": {"current": "idle"}}
    principal["subordinates"] = {"router/0": router}
    lines.append(json.dumps({"ts": "t3b", "status": {"applications": {"valkey": {"units": {"valkey/0": principal}}}}}))
    # a torn-down model, a hostile shape and a line truncated by a killed watcher
    lines += [json.dumps({"ts": "t4", "status": {"applications": {}}}), '{"status": {"applications": {"a": []}}}', '{"ts": "t5", "sta']
    status.write_text("\n".join(lines))
    assert classify.unit_timeline(status) == [
        "t1 valkey/0: workload maintenance (), agent idle",
        "t3 valkey/0: workload error (hook failed), agent idle",
        "t3b router/0: workload error (), agent idle",
    ]


def test_top_processes_reports_peaks_and_restarts():
    mib = 2**20
    procstat = [
        _metric("procstat", {"process_name": "mongod"}, {"pid": 10, "memory_rss": 900 * mib, "cpu_usage": 50.0}, 1_000),
        _metric("procstat", {"process_name": "mongod"}, {"pid": 10, "memory_rss": 1200 * mib, "cpu_usage": 20.0}, 2_000),
        # restarted: a second pid
        _metric("procstat", {"process_name": "mongod"}, {"pid": 11, "memory_rss": 100 * mib, "cpu_usage": 5.0}, 3_000),
        _metric("procstat", {"process_name": "jujud"}, {"pid": 20, "memory_rss": 50 * mib, "cpu_usage": 190.0}, 3_000),
        # never busy: its peak CPU time is its first sample, not the epoch
        _metric("procstat", {"process_name": "tail"}, {"pid": 30, "memory_rss": 1 * mib, "cpu_usage": 0.0}, 4_000),
    ]
    assert "peak CPU 0% at 00:00:04Z" in classify.top_processes({"procstat": procstat})
    out = classify.top_processes({"procstat": procstat}, n=1).splitlines()
    assert out == [
        "mongod (pid 10): peak RSS 1200 MB at 00:00:02Z, peak CPU 50% at 00:00:01Z",
        "jujud (pid 20): peak RSS 50 MB at 00:00:03Z, peak CPU 190% at 00:00:03Z",
        "Pids seen per process name (a restarted process or a new log follower gets a new pid): mongod: 2",
    ]


def test_build_prompt_clips_each_section():
    job = {"id": 1, "name": "integration", "runner_name": "r"}
    prompt = classify.build_prompt(job, "L" * 200_000, None, "D" * 200_000)
    assert len(prompt) <= classify.PROMPT_BUDGET
    assert "no collect bundle found" in prompt
    assert prompt.count("characters cut") == 2


def test_bundles_match_by_runner_name_and_attempt(tmp_path):
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    mem = {"available_percent": 50.0}
    _bundle(bundles / "11", [_metric("mem", {}, dict(mem), 1_000)], runner="GitHub Actions 7", attempt="2")
    _bundle(bundles / "12", [_metric("mem", {}, dict(mem), 1_000)], runner="GitHub Actions 7", attempt="1")
    # one runner, two bundles in this attempt: neither can be trusted
    _bundle(bundles / "13", [_metric("mem", {}, dict(mem), 1_000)], runner="self-hosted", attempt="2")
    _bundle(bundles / "14", [_metric("mem", {}, dict(mem), 1_000)], runner="self-hosted", attempt="2")
    (bundles / "15").mkdir()  # a plot artifact holds no metrics
    assert classify.bundles_by_runner(bundles, "2") == {"GitHub Actions 7": str(bundles / "11")}


def test_parse_verdict_keeps_only_known_keys_and_types():
    good = classify.parse_verdict('```json\n{"verdict": "flaky", "confidence": 0.8}\n```')
    assert good["verdict"] == "flaky"
    assert good["confidence"] == 0.8
    hostile = classify.parse_verdict(
        json.dumps({"verdict": "real", "job": "spoofed", "confidence": "high", "reasoning": ["a"], "evidence": "one"})
    )
    assert "job" not in hostile
    assert hostile["confidence"] is None
    assert hostile["reasoning"] == "['a']"
    assert hostile["evidence"] == ["one"]
    assert classify.parse_verdict('{"verdict": "flaky", "confidence": NaN}')["confidence"] is None
    assert classify.parse_verdict('{"verdict": "flaky", "confidence": 7}')["confidence"] is None
    bad = classify.parse_verdict("I think it is flaky")
    assert bad == {"verdict": "inconclusive", "raw": "I think it is flaky"}
    assert classify.parse_verdict('{"verdict": "maybe"}')["verdict"] == "inconclusive"
    assert classify.parse_verdict([{"type": "text"}])["verdict"] == "inconclusive"


def test_main_writes_verdicts_and_summary(tmp_path, monkeypatch):
    args = _main_args(tmp_path, [{"id": 5, "name": "smoke", "runner_name": "r"}])
    (tmp_path / "logs" / "5.log").write_text("FAILED test_x - AssertionError\n")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    seen = {}

    def fake_call(prompt, model, key):
        seen["prompt"] = prompt
        return '{"verdict": "real", "confidence": 0.9, "category": "test-bug", "reasoning": "r", "evidence": ["e"]}'

    monkeypatch.setattr(classify, "call_openrouter", fake_call)
    assert classify.main([*args, "--diff", str(tmp_path / "none")]) == 0
    assert "FAILED test_x" in seen["prompt"]
    verdicts = json.loads((tmp_path / "out" / "verdicts.json").read_text())
    assert verdicts[0]["verdict"] == "real"
    assert verdicts[0]["model"] == "m"
    assert "| smoke | **real** | 0.9 | test-bug |" in summary.read_text()


def test_main_survives_a_bad_reply_and_classifies_the_rest(tmp_path, monkeypatch):
    args = _main_args(tmp_path, [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])
    replies = iter([TypeError("'NoneType' object is not subscriptable"), '{"verdict": "flaky"}'])

    def fake_call(prompt, model, key):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(classify, "call_openrouter", fake_call)
    assert classify.main(args) == 0
    verdicts = json.loads((tmp_path / "out" / "verdicts.json").read_text())
    assert [v["verdict"] for v in verdicts] == ["inconclusive", "flaky"]
    assert verdicts[0]["error"].startswith("TypeError")


def test_main_survives_a_hostile_bundle(tmp_path, monkeypatch):
    args = _main_args(tmp_path, [{"id": 1, "name": "a", "runner_name": "r"}])
    hostile = tmp_path / "bundles" / "9"
    hostile.mkdir()
    (hostile / "metrics.json").write_text(
        '{"metrics": [{"name": ["x"], "tags": {}, "fields": {}, "timestamp": 1}]}\n'
        + '{"metrics": [' + "9" * 5000 + "]}\n"
        + "[" * 100_000 + "\n"
    )
    # the matched bundle has a timestamp no clock can format
    _bundle(tmp_path / "bundles" / "10", [_metric("mem", {}, {"available_percent": 1.0}, 1e20)], runner="r")
    (tmp_path / "logs" / "1.log").write_text("FAILED test_x\n")
    seen = {}

    def fake_call(prompt, model, key):
        seen["prompt"] = prompt
        return '{"verdict": "flaky", "confidence": 0}'

    monkeypatch.setattr(classify, "call_openrouter", fake_call)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert classify.main([*args, "--attempt", "1"]) == 0
    assert "FAILED test_x" in seen["prompt"]
    assert "the collect bundle could not be read" in seen["prompt"]
    assert json.loads((tmp_path / "out" / "verdicts.json").read_text())[0]["verdict"] == "flaky"
    assert "| a | **flaky** | 0 |" in summary.read_text()
