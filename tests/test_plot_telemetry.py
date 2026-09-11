import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "actions"
    / "plot"
    / "scripts"
    / "plot_telemetry.py"
)
_spec = importlib.util.spec_from_file_location("plot_telemetry", _SCRIPT_PATH)
plot_telemetry = importlib.util.module_from_spec(_spec)
sys.modules["plot_telemetry"] = plot_telemetry
_spec.loader.exec_module(plot_telemetry)


def _metric(name, tags, fields, ts):
    return {"name": name, "tags": tags, "fields": fields, "timestamp": ts}


def test_xy_skips_records_missing_the_field():
    # Real `system` samples split load1/load5/n_cpus from uptime from
    # uptime_format -- a bare field access would KeyError on 2 of 3 records.
    records = [
        _metric("system", {}, {"load1": 0.1, "load5": 0.2, "n_cpus": 4}, 1000),
        _metric("system", {}, {"uptime": 42}, 1005),
        _metric("system", {}, {"uptime_format": "0:01"}, 1010),
    ]
    xs, ys = plot_telemetry.xy(records, "load1")
    assert len(xs) == 1
    assert ys == [0.1]


def test_rate_clamps_counter_reset_to_zero():
    records = [
        _metric("diskio", {"name": "sda"}, {"read_bytes": 1000}, 0),
        _metric("diskio", {"name": "sda"}, {"read_bytes": 2000}, 1000),
        # counter reset (e.g. device replaced): must not go negative
        _metric("diskio", {"name": "sda"}, {"read_bytes": 500}, 2000),
    ]
    _, ys = plot_telemetry.rate(records, "read_bytes")
    assert ys[0] == 1000.0
    assert ys[1] == 0.0


def test_load_metrics_skips_malformed_lines(tmp_path):
    path = tmp_path / "metrics.json"
    good = json.dumps({"metrics": [_metric("cpu", {"cpu": "cpu-total"}, {"usage_user": 1.0}, 1000)]})
    path.write_text(
        good
        + "\n"
        + "not json at all\n"
        + json.dumps({"metrics": "not-a-list"})
        + "\n"
        + '{"metrics": [{"name": "swap"}]}\n'  # missing timestamp -> skipped
    )
    loaded = plot_telemetry.load_metrics([str(path)])
    assert loaded.ts_min == 1000
    assert len(loaded.by_name["cpu"]) == 1


def test_load_metrics_reads_multiple_files(tmp_path):
    f1 = tmp_path / "metrics.json"
    f2 = tmp_path / "metrics.2026-01-01-1700000000.json"
    f1.write_text(json.dumps({"metrics": [_metric("mem", {}, {"used_percent": 1.0}, 1000)]}) + "\n")
    f2.write_text(json.dumps({"metrics": [_metric("mem", {}, {"used_percent": 2.0}, 2000)]}) + "\n")
    loaded = plot_telemetry.load_metrics([str(f1), str(f2)])
    assert len(loaded.by_name["mem"]) == 2
    assert loaded.ts_min == 1000
    assert loaded.ts_max == 2000


def test_group_by_pid_field_not_tag():
    # pid lives in `fields`, not `tags` -- two pids sharing a process_name
    # tag must produce two distinct groups, not one collapsed group.
    records = [
        _metric("procstat", {"process_name": "containerd"}, {"pid": 100, "memory_rss": 1}, 1000),
        _metric("procstat", {"process_name": "containerd"}, {"pid": 200, "memory_rss": 2}, 1000),
    ]
    groups = plot_telemetry.group_by(records, lambda m: (m["tags"]["process_name"], m["fields"]["pid"]))
    assert set(groups.keys()) == {("containerd", 100), ("containerd", 200)}
    assert len(groups[("containerd", 100)]) == 1
    assert len(groups[("containerd", 200)]) == 1


def test_is_whole_disk():
    assert plot_telemetry.is_whole_disk("sda") is True
    assert plot_telemetry.is_whole_disk("nvme0n1") is True
    assert plot_telemetry.is_whole_disk("sda1") is False
    assert plot_telemetry.is_whole_disk("nvme0n1p1") is False
    assert plot_telemetry.is_whole_disk("dm-0") is False


def test_is_primary_interface():
    assert plot_telemetry.is_primary_interface("eth0") is True
    assert plot_telemetry.is_primary_interface("enP42298s1") is True
    assert plot_telemetry.is_primary_interface("docker0") is False
    assert plot_telemetry.is_primary_interface("lxdbr0") is False
    assert plot_telemetry.is_primary_interface("br-abc123") is False


def test_lifeline_spans_minimum_bar_width():
    # A process sampled exactly once must still get a visible (non-zero-width) bar.
    records_by_key = {
        ("pytest", 999): [_metric("procstat", {"process_name": "pytest"}, {"pid": 999}, 5000)],
    }
    spans = plot_telemetry.lifeline_spans(records_by_key)
    assert len(spans) == 1
    label, _start, width = spans[0]
    assert label == "pytest:999"
    assert width >= timedelta(seconds=30)


def test_build_title_derived_from_tags():
    loaded = plot_telemetry.Loaded()
    loaded.common_tags = {
        "repo": "canonical/flake0",
        "workflow": "smoke-collect",
        "job": "smoke",
        "run_id": "123",
        "sha": "abcdef1234567890",
        "ref": "main",
        "runner_name": "runner-1",
    }
    title, subtitle = plot_telemetry.build_title(loaded)
    assert "canonical/flake0" in title
    assert "smoke-collect" in title
    assert "smoke" in title
    assert "123" in subtitle
    assert "abcdef12" in subtitle
    assert "main" in subtitle
    assert "runner-1" in subtitle
