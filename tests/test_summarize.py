"""Tests for summarize.py. tests/golden holds comments and job summaries
rendered by the first classify version (PR #10), and the format must not
change."""

import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
GOLDEN = REPO / "tests/golden"
sys.path.insert(0, str(REPO / ".github/actions/summarize/scripts"))
import summarize

RUN_URL = "https://github.com/o/r/actions/runs/9"


@pytest.mark.parametrize("name", ["flaky", "mix", "none"])
def test_comment_matches_golden(name, tmp_path):
    out = tmp_path / "comment.md"
    summarize.main(["--verdicts", str(GOLDEN / name / "verdicts"), "--run-url", RUN_URL,
                    "--marker", "<!-- flake0 -->", "--out", str(out)])
    assert out.read_text() == (GOLDEN / name / "comment.md").read_text()


@pytest.mark.parametrize("path", sorted(GOLDEN.glob("*/verdicts/*/verdict.json")), ids=str)
def test_job_summary_matches_golden(path):
    golden = path.parents[2] / f"job-summary-{path.parent.name}.md"
    assert summarize.job_summary(json.loads(path.read_text())) == golden.read_text()


def test_unknown_labels_never_reach_the_comment():
    group = summarize.job_group({"job": "j", "verdict": "PWNED", "root_cause": "[x](http://e)",
                                 "recommendation": "<img src=x>", "job_id": "1) [y](http://e"})
    assert (group["verdict"], group["root_cause"], group["recommendation"], group["job_id"]) == (
        "INCONCLUSIVE", "unknown", "needs_human_review", "")
