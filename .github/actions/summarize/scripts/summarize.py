#!/usr/bin/env python3
"""Render the flake0 pull request comment and job summary from classify verdicts.

    summarize.py --verdicts <dir> --run-url <url> --marker <text> --out <file>
    summarize.py --job-summary <verdict.json>

<dir> holds one verdict.json per failed job, at any depth. --job-summary prints
one job's summary table to stdout.

Verdicts come from a model reading pull request logs, so they are untrusted:
the comment shows known labels only, and other text as escaped, one-line text.
"""

import argparse
import json
import sys
from pathlib import Path

VERDICTS = ("REAL_FAILURE", "FLAKY", "INCONCLUSIVE")
ROOT_CAUSES = (
    "code_regression", "code_preexisting", "test_defect", "workload_defect",
    "environment_infra", "dependency_drift", "external_service", "unknown",
)
RECOMMENDATIONS = (
    "block_merge", "open_issue", "fix_test", "quarantine_test", "retry_safe",
    "escalate_infra", "needs_human_review",
)
# Worst first: (key, section title, headline name)
BUCKETS = [
    ("charm", "Charm bugs", "charm bugs"),
    ("test", "Test issues", "test issues"),
    ("flaky", "Flaky", "flakiness"),
    ("none", "No verdict", None),
]
ROOT_CAUSE_BUCKET = {
    "code_regression": "charm",
    "code_preexisting": "charm",
    "workload_defect": "charm",
    "test_defect": "test",
    "environment_infra": "flaky",
    "external_service": "flaky",
    "dependency_drift": "flaky",
}


def load_verdicts(directory):
    """Every readable verdict.json under directory that names its job, sorted by job."""
    verdicts = []
    for path in sorted(Path(directory).rglob("verdict.json")):
        try:
            verdict = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(verdict, dict) and verdict.get("job"):
            verdicts.append(verdict)
    return sorted(verdicts, key=lambda v: str(v["job"]))


def known(value, allowed, default):
    return value if value in allowed else default


def job_group(verdict):
    """One row of the comment from a verdict, with known values only."""
    tests = verdict.get("failed_tests")
    tests = tests if isinstance(tests, list) else [tests] if tests else []
    return {
        "verdict": known(verdict.get("verdict"), VERDICTS, "INCONCLUSIVE"),
        "root_cause": known(verdict.get("root_cause"), ROOT_CAUSES, "unknown"),
        "recommendation": known(verdict.get("recommendation"), RECOMMENDATIONS, "needs_human_review"),
        "failed_tests": [str(t) for t in tests],
        "summary": str(verdict.get("summary") or "") or "no classify verdict",
        "suggested_fix": str(verdict.get("suggested_fix") or ""),
        "label": str(verdict["job"]),
        "job_id": job_id(verdict),
    }


def job_id(verdict):
    """The job's check run id, digits only: verdict files are untrusted and it ends up in a link."""
    value = str(verdict.get("job_id") or "")
    return value if value.isdigit() else ""


def bucket(group):
    if group["verdict"] == "INCONCLUSIVE":
        return "none"
    return ROOT_CAUSE_BUCKET.get(group["root_cause"], "none")


def safe(text, limit=300):
    """One line of plain text: no HTML, links, images, mentions or issue references, no code span to break out of."""
    text = " ".join(str(text).split())[:limit]
    # & first: entities like &commat; would render as the characters escaped below
    for raw, escaped in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ("[", "\\["), ("]", "\\]"), ("`", "'"),
                         ("@", "@\u200b"), ("#", "#\u200b"), ("://", ":\u200b//"), ("www.", "www\u200b.")):
        text = text.replace(raw, escaped)
    return text


def code(text, limit=120):
    """Text for inside a code span, where GitHub renders nothing but a backtick can end the span."""
    return " ".join(str(text).split())[:limit].replace("`", "'")


def test_link(group, run_url):
    """The failed tests (or the job) as a link to the job's summary on the run page."""
    # The job name when no test is named: setup failures name none
    name = ", ".join(code(t) for t in group["failed_tests"][:3]) or code(group["label"])
    link = f"[`{name}`]({run_url}#summary-{group['job_id']})" if group["job_id"] else f"`{name}`"
    # | would split the table cell, even inside a code span
    return link.replace("|", "\\|")


def render(groups, run_url, marker):
    by_bucket = {key: [g for g in groups if bucket(g) == key] for key, _, _ in BUCKETS}
    present = [name for key, _, name in BUCKETS if name and by_bucket[key]]
    if not present:
        headline = "Integration tests failing. flake0 could not classify the failures."
    elif len(present) == 1:
        headline = f"Integration tests failing due to {present[0]}."
    else:
        headline = f"Integration tests failing due to a mix of {', '.join(present[:-1])} and {present[-1]}."
    out = [marker, f"**flake0:** {headline} [Run summary]({run_url})", ""]
    out += ["| Category | Count | Failing tests |", "|---|---|---|"]
    out += [f"| {title} | {len(by_bucket[key])} | {', '.join(test_link(g, run_url) for g in by_bucket[key])} |"
            for key, title, _ in BUCKETS if by_bucket[key]]
    fixes = [g for key in ("charm", "test") for g in by_bucket[key]]
    if fixes:
        out += ["", "**To fix**"]
        for g in fixes:
            line = f"- {test_link(g, run_url)}: {safe(g['summary'])}".rstrip(".")
            line += f". Recommendation: {g['recommendation'].replace('_', ' ')}."
            if g["suggested_fix"]:
                line += f" Suggested fix: {safe(g['suggested_fix'])}"
            out.append(line)
    out += ["", "_AI-generated by flake0. It can be wrong, so check before acting on it._"]
    return "\n".join(out) + "\n"


def job_summary(r):
    """The job summary table. Only the job's own readers see it, so text is not escaped."""
    confidence = "" if r.get("confidence_pct") is None else f"{r['confidence_pct']} ({r.get('confidence_bucket', '')})"
    # Matrix job names hold a '|', which would split a table cell
    job = str(r.get("job", "")).replace("|", "\\|")
    rows = ["## flake0 classify", "", "| Job | Verdict | Root cause | Confidence | Recommendation |", "|---|---|---|---|---|"]
    rows += [f"| {job} | **{r.get('verdict', 'INCONCLUSIVE')}** | {r.get('root_cause', '')} | {confidence} | {r.get('recommendation', '')} |", ""]
    rows += [r.get("reasoning") or r.get("error") or "(no reasoning)"]
    if r.get("suggested_fix"):
        rows += ["", f"Suggested fix: {r['suggested_fix']}"]
    rows += [f"- {e}" for e in r.get("evidence") or []]
    return "\n".join(rows) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="summarize.py", description=__doc__.splitlines()[0])
    parser.add_argument("--job-summary")
    parser.add_argument("--verdicts")
    parser.add_argument("--run-url")
    parser.add_argument("--marker")
    parser.add_argument("--out")
    args = parser.parse_args(argv)

    if args.job_summary:
        verdict = json.loads(Path(args.job_summary).read_text(encoding="utf-8", errors="replace"))
        sys.stdout.write(job_summary(verdict if isinstance(verdict, dict) else {}))
        return 0
    verdicts = load_verdicts(args.verdicts)
    if not verdicts:
        print("summarize: no verdicts", file=sys.stderr)
        return 1
    groups = [job_group(v) for v in verdicts]
    Path(args.out).write_text(render(groups, args.run_url, args.marker), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
