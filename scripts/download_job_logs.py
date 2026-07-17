#!/usr/bin/env python3
"""Download raw logs and artifacts for a GitHub Actions run.

Requires:
    export GITHUB_TOKEN="ghp_..."
    pip install "ghapi>=2,<3"

Usage:
    python3 download_job_logs.py owner/repo RUN_ID
    python3 download_job_logs.py owner/repo RUN_ID --attempt 2
    python3 download_job_logs.py owner/repo RUN_ID -o ./logs
    python3 download_job_logs.py owner/repo RUN_ID --skip-artifacts
    python3 download_job_logs.py owner/repo RUN_ID -c 10
    python3 download_job_logs.py owner/repo RUN_ID -q

Options:
    --attempt N        Specific run attempt number (default: latest)
    -o, --output DIR   Output directory (default: current dir)
    -c, --concurrency  Max parallel downloads (default: 5)
    --skip-artifacts   Only download raw logs, skip workflow artifacts
    -q, --quiet        Suppress info messages, show errors only

Exit codes:
    0  all requested files downloaded successfully
    1  one or more files failed, or the run could not be queried

Output filenames:
    {repo}_run{RUN_ID}_job{JOB_ID}_attempt{N}_{status}_{job-name}_logs.txt
    {repo}_run{RUN_ID}_art{ARTIFACT_ID}_{artifact-name}.zip
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from functools import partial
from pathlib import Path

import httpx
from ghapi.all import GhApi, paged

logger = logging.getLogger(__name__)

# ghapi HTTP errors subclass urllib.error.HTTPError -> OSError, transport errors
# surface as httpx.HTTPError, and file writes raise OSError. All are treated as
# recoverable download failures rather than bugs.
DOWNLOAD_ERRORS = (httpx.HTTPError, OSError)

DEFAULT_CONCURRENCY = 5


class Abort(Exception):
    """Fatal, user-facing error; handled at the entry point as exit code 1."""


def sanitize(name: str) -> str:
    """Replace characters unsafe for filenames with ``_``."""
    return re.sub(r"[^\w\-.]", "_", name)


def write_file(dest: Path, data: str | bytes) -> None:
    """Write *data* to *dest*, creating parent directories as needed."""
    if isinstance(data, str):
        data = data.encode()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    logger.info("  -> %s (%.1f KB)", dest, len(data) / 1024)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments and configure logging."""
    parser = argparse.ArgumentParser(
        description="Download GitHub Actions run logs and artifacts",
    )
    parser.add_argument("repo", help="Repository in OWNER/REPO format")
    parser.add_argument("run_id", type=int, help="GitHub Actions run ID")
    parser.add_argument("--attempt", type=int, help="Run attempt (default: latest)")
    parser.add_argument("-o", "--output", default=".", help="Output directory")
    parser.add_argument("-c", "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Max parallel downloads (default: {DEFAULT_CONCURRENCY})")
    parser.add_argument("--skip-artifacts", action="store_true",
                        help="Only download raw logs, skip artifacts")
    parser.add_argument("-q", "--quiet", action="store_true", help="Show errors only")
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )
    return args


async def list_all(oper, key: str, **kwargs) -> list:
    """Fetch every item across all pages of an enveloped GitHub list endpoint.

    These endpoints wrap results as ``{"total_count": N, "<key>": [...]}``,
    which is never falsy, so ``paged`` is fed the inner list instead so its
    empty-page stop condition works.
    """
    async def page_items(**kw):
        return getattr(await oper(**kw), key)

    items: list = []
    async for page in paged(page_items, per_page=100, **kwargs):
        items += page
    return items


async def download_all(specs: list[tuple], sem: asyncio.Semaphore) -> tuple[int, int]:
    """Download ``(label, dest, fetch)`` specs in parallel. Returns (ok, total)."""
    total = len(specs)

    async def run(index: int, label: str, dest: Path, fetch) -> bool:
        async with sem:
            logger.info("[%d/%d] %s", index, total, label)
            try:
                write_file(dest, await fetch())
            except DOWNLOAD_ERRORS as exc:
                logger.error("  Failed: %s: %s", dest, exc)
                return False
        return True

    done = await asyncio.gather(*(run(i, *s) for i, s in enumerate(specs, 1)))
    return sum(done), total


async def main() -> int:
    """Download job logs and artifacts for a run. Returns a process exit code."""
    args = parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise Abort("GITHUB_TOKEN environment variable not set")

    owner, _, name = args.repo.partition("/")
    if not owner or not name:
        raise Abort("repo must be in OWNER/REPO format")

    slug = sanitize(args.repo)
    out_dir = Path(args.output)
    api = GhApi(owner=owner, repo=name, token=token)
    sem = asyncio.Semaphore(args.concurrency)

    run = await api.actions.get_workflow_run(run_id=args.run_id)
    attempt = args.attempt if args.attempt is not None else run.run_attempt
    logger.info("Run: %s #%d  attempt: %d", args.repo, args.run_id, attempt)

    # Note: the REST API only serves job logs for the *latest* attempt, so for
    # older attempts the contents may not match the encoded attempt.
    jobs = await list_all(
        api.actions.list_jobs_for_workflow_run_attempt,
        "jobs", run_id=args.run_id, attempt_number=attempt)
    logger.info("Found %d job(s)", len(jobs))
    job_specs = [
        (
            f"Job '{j.name}' (id={j.id}, status={j.conclusion or 'unknown'})",
            out_dir / (f"{slug}_run{args.run_id}_job{j.id}_attempt{attempt}"
                       f"_{j.conclusion or 'unknown'}_{sanitize(j.name)}_logs.txt"),
            partial(api.actions.download_job_logs_for_workflow_run, job_id=j.id),
        )
        for j in jobs
    ]
    log_ok, log_total = await download_all(job_specs, sem)
    logger.info("Downloaded %d/%d job log(s)", log_ok, log_total)

    art_ok = art_total = 0
    if args.skip_artifacts:
        logger.info("Skipping artifacts (--skip-artifacts).")
    else:
        artifacts = await list_all(
            api.actions.list_workflow_run_artifacts,
            "artifacts", run_id=args.run_id)
        logger.info("Found %d artifact(s)", len(artifacts))
        art_specs = [
            (
                f"Artifact '{a.name}' (id={a.id})",
                out_dir / f"{slug}_run{args.run_id}_art{a.id}_{sanitize(a.name)}.zip",
                partial(api.actions.download_artifact, artifact_id=a.id,
                        archive_format="zip"),
            )
            for a in artifacts
        ]
        art_ok, art_total = await download_all(art_specs, sem)
        logger.info("Downloaded %d/%d artifact(s)", art_ok, art_total)

    failures = (log_total - log_ok) + (art_total - art_ok)
    if failures:
        logger.warning("Done with %d failure(s).", failures)
        return 1
    logger.info("Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        logger.error("Interrupted")
        sys.exit(130)
    except (Abort, *DOWNLOAD_ERRORS) as exc:
        logger.error("Error: %s", exc)
        sys.exit(1)
