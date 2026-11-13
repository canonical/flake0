"""Tests for the classify action's run.sh, with tests/fakes/opencode."""

import json
import os
import pathlib
import subprocess
import tarfile

REPO = pathlib.Path(__file__).resolve().parents[1]
RUN = REPO / ".github/actions/classify/scripts/run.sh"
FAKE = REPO / "tests/fakes/opencode"

VERDICT = {"verdict": "FLAKY", "root_cause": "environment_infra", "recommendation": "retry_safe",
           "confidence_pct": 80, "confidence_bucket": "high", "failed_tests": ["test_x"],
           "summary": "PSI io peaked.", "suggested_fix": "", "reasoning": "IO stall.", "evidence": ["metrics"]}


def run(tmp_path, bundle_files=(), symlink=None, **env):
    """Run run.sh on a bundle of {name: text} and return (verdict, work dir, step summary, stdout)."""
    src = tmp_path / "bundle-src"
    src.mkdir()
    for name, text in dict(bundle_files).items():
        (src / name).write_text(text)
    if symlink:
        (src / "link.log").symlink_to(symlink)
    with tarfile.open(tmp_path / "bundle.tar", "w") as tar:
        for p in src.iterdir():
            tar.add(p, arcname=p.name)
    temp = tmp_path / "temp"
    temp.mkdir()
    out = tmp_path / "out"
    summary = tmp_path / "summary.md"
    proc = subprocess.run(
        [RUN], capture_output=True, text=True, timeout=60, check=True,
        env={**os.environ, "RUNNER_TEMP": str(temp), "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary),
             "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "9", "GITHUB_WORKSPACE": str(REPO),
             "OPENROUTER_API_KEY": "sk-test", "GH_TOKEN": "gh-test", "OPENCODE_BIN": str(FAKE),
             "BUNDLE_PATH": str(tmp_path / "bundle.tar"), "JOB_NAME": "integration (lxd)", "MODEL": "m/x",
             "OPENCODE_CONFIG_CONTENT": '{"model": "planted"}', **env})
    path = out.read_text().strip().removeprefix("verdict-path=")
    verdict = json.loads(pathlib.Path(path).read_text()) if path else None
    work = next(temp.glob("flake0-classify-work.*"), None)
    return verdict, work, summary.read_text() if summary.exists() else "", proc.stdout


def test_verdict_gets_job_and_model(tmp_path):
    verdict, _, summary, stdout = run(tmp_path, {"fake-verdict": json.dumps(VERDICT)})
    assert verdict == {**VERDICT, "job": "integration (lxd)", "model": "m/x"}
    assert "| integration (lxd) | **FLAKY** | environment_infra | 80 (high) | retry_safe |" in summary
    assert '"verdict": "FLAKY"' in stdout


def test_no_verdict_becomes_an_error_verdict(tmp_path):
    verdict, *_ = run(tmp_path)
    assert verdict == {"job": "integration (lxd)", "model": "m/x", "verdict": "INCONCLUSIVE",
                       "error": "opencode exited 0 without writing a verdict"}


def test_verdict_from_the_last_message_at_the_steps_cap(tmp_path):
    message = f'Out of steps. Notes: {{"a": 1}}\n```json\n{json.dumps(VERDICT)}\n```\nWrite it to verdict.json.'
    verdict, *_ = run(tmp_path, {"fake-message": message})
    assert verdict == {**VERDICT, "job": "integration (lxd)", "model": "m/x"}


def test_follow_up_asks_the_same_session_for_the_verdict(tmp_path):
    verdict, work, *_ = run(tmp_path, {"fake-session": "", "fake-follow-up-verdict": json.dumps(VERDICT)})
    assert verdict == {**VERDICT, "job": "integration (lxd)", "model": "m/x"}
    argv = json.loads((work / "fake-follow-up-argv.json").read_text())
    assert argv[argv.index("--session") + 1] == "ses_fake" and "Write verdict.json now" in argv[-1]


def test_opencode_error_event_lands_in_the_verdict(tmp_path):
    verdict, *_ = run(tmp_path, {"fake-error": ""})
    assert verdict["error"] == "opencode exited 1 without writing a verdict: Model unavailable"


def test_bad_json_and_timeout_become_error_verdicts(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    verdict, *_ = run(tmp_path / "a", {"fake-verdict": "[1]"})
    assert verdict["error"] == "the verdict is not a JSON object"
    verdict, *_ = run(tmp_path / "b", {"fake-sleep": ""}, FLAKE0_CLASSIFY_TIMEOUT="1s")
    assert verdict["error"] == "opencode timed out after 1s"


def test_opencode_gets_a_clean_environment(tmp_path):
    _, work, *_ = run(tmp_path)
    env = json.loads((work / "fake-env.json").read_text())
    assert env["OPENROUTER_API_KEY"] == "sk-test" and env["GH_TOKEN"] == "gh-test"
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert "OPENCODE_CONFIG_CONTENT" not in env and "GITHUB_WORKSPACE" not in env
    assert env["HOME"].startswith(str(tmp_path))
    argv = json.loads((work / "fake-argv.json").read_text())
    assert argv[:10] == ["run", "--standalone", "--agent", "flaky-test-investigator", "--model", "openrouter/m/x",
                        "--format", "json", "--title", "flake0 classify"]
    assert "bundle/: the job's collect bundle" in argv[-1] and "You have 45 steps" in argv[-1]


def test_work_dir_has_no_links_and_one_metric_per_line(tmp_path):
    metrics = json.dumps({"metrics": [{"name": "cpu"}, {"name": "mem"}]})
    _, work, *_ = run(tmp_path, {"job.log": "log", "metrics.json": metrics}, symlink="/proc/self/environ")
    files = json.loads((work / "fake-files.json").read_text())
    assert "bundle/link.log" not in files and "bundle/metrics.json" not in files
    assert {"bundle/job.log", "bundle/metrics.ndjson", "docs/taxonomy.md", "src/README.md"} <= set(files)
    assert (work / "bundle/metrics.ndjson").read_text() == '{"name":"cpu"}\n{"name":"mem"}\n'


def test_no_key_skips(tmp_path):
    verdict, work, *_ = run(tmp_path, OPENROUTER_API_KEY="")
    assert verdict is None and work is None
