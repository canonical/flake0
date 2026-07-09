#!/usr/bin/env python3
"""Download raw logs and artifacts for a GitHub Actions run.

Requires GITHUB_TOKEN environment variable.
    export GITHUB_TOKEN="ghp_..."

Usage:
    python3 download_job_logs.py owner/repo 12345678
    python3 download_job_logs.py owner/repo 12345678 --attempt 2 -o ./logs
"""

import os
import sys
import json
import urllib.request
import urllib.error
import urllib.parse
import argparse
from pathlib import Path

GITHUB_API = "https://api.github.com"


def _build_opener(token):
    """Return an opener that preserves headers across same-domain redirects only."""
    class _PreserveAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
            if urllib.parse.urlparse(req.full_url).netloc == urllib.parse.urlparse(newurl).netloc:
                for name in ("Authorization", "Accept", "User-Agent"):
                    if name in req.headers:
                        new_req.add_header(name, req.headers[name])
            else:
                new_req.add_header("User-Agent", req.headers.get("User-Agent", "download-job-logs/1.0"))
            return new_req

    return urllib.request.build_opener(_PreserveAuthRedirectHandler())


def _do_request(url, token, opener):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "download-job-logs/1.0")
    return opener.open(req)


def api_request(url, token, opener):
    """Make a GitHub API request, returning parsed JSON."""
    try:
        with _do_request(url, token, opener) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"HTTP {e.code} for {url}: {body}", file=sys.stderr)
        sys.exit(1)


def api_request_paginated(url, token, opener):
    """Fetch all pages from a GitHub API list endpoint."""
    results = []
    page = 1
    item_keys = {"items", "jobs", "artifacts", "workflow_runs", "secrets", "variables"}
    while True:
        paginated_url = f"{url}{'&' if '?' in url else '?'}page={page}&per_page=100"
        data = api_request(paginated_url, token, opener)
        if isinstance(data, list):
            results.extend(data)
            if len(data) < 100:
                break
        elif isinstance(data, dict):
            extracted = False
            for key in item_keys:
                if key in data:
                    results.extend(data[key])
                    extracted = True
                    if len(data[key]) < 100:
                        return results
                    break
            if not extracted:
                results.append(data)
                break
        else:
            break
        page += 1
    return results


def download_file(url, token, dest):
    """Download a file, manually following cross-domain redirects without auth."""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    no_redirect_opener = urllib.request.build_opener(_NoRedirect)

    try:
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"token {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "download-job-logs/1.0")

        try:
            resp = no_redirect_opener.open(req)
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                raise
            redirect_url = e.headers.get("Location")
            if not redirect_url:
                raise ValueError("Redirect with no Location header")
        else:
            if resp.status in (301, 302, 303, 307, 308):
                redirect_url = resp.headers.get("Location")
                if not redirect_url:
                    raise ValueError("Redirect with no Location header")
            else:
                data = resp.read()
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(data)
                size_kb = len(data) / 1024
                print(f"  -> {dest} ({size_kb:.1f} KB)")
                return

        req2 = urllib.request.Request(redirect_url)
        req2.add_header("User-Agent", "download-job-logs/1.0")
        with urllib.request.urlopen(req2) as resp2:
            data = resp2.read()

    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"  URL: {url}", file=sys.stderr)
        print(f"  HTTP {e.code} - {e.reason}", file=sys.stderr)
        print(f"  Response headers: {dict(e.headers)}", file=sys.stderr)
        print(f"  Response body: {body}", file=sys.stderr)
        raise

    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)

    size_kb = len(data) / 1024
    print(f"  -> {dest} ({size_kb:.1f} KB)")


def sanitize(name):
    return name.replace("/", "_").replace("\\", "_").replace(" ", "_")


def main():
    parser = argparse.ArgumentParser(
        description="Download GitHub Actions run logs and artifacts"
    )
    parser.add_argument("repo", help="Repository in OWNER/REPO format")
    parser.add_argument("run_id", type=int, help="GitHub Actions run ID")
    parser.add_argument(
        "--attempt", type=int, default=None,
        help="Specific run attempt (default: latest)"
    )
    parser.add_argument(
        "-o", "--output", default=".",
        help="Output directory (default: current dir)"
    )
    parser.add_argument(
        "--skip-artifacts", action="store_true",
        help="Only download raw logs, skip artifacts"
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Error: GITHUB_TOKEN environment variable not set", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    repo_slug = sanitize(args.repo)

    opener = _build_opener(token)

    # --- Fetch run info ---
    run_url = f"{GITHUB_API}/repos/{args.repo}/actions/runs/{args.run_id}"
    run_info = api_request(run_url, token, opener)
    attempt = args.attempt or run_info.get("run_attempt", 1)

    # --- Fetch jobs ---
    if args.attempt:
        jobs_url = f"{run_url}/attempts/{attempt}/jobs"
    else:
        jobs_url = f"{run_url}/jobs"

    print(f"Run: {args.repo} #{args.run_id}  attempt: {attempt}")
    print(f"Fetching jobs...")
    jobs = api_request_paginated(jobs_url, token, opener)
    print(f"  Found {len(jobs)} job(s)\n")

    # --- Download raw logs ---
    for job in jobs:
        job_id = job["id"]
        job_name = sanitize(job["name"])
        job_status = job.get("conclusion", "unknown")

        fname = (
            f"{repo_slug}_run{args.run_id}_job{job_id}"
            f"_attempt{attempt}_{job_status}_{job_name}_logs.txt"
        )

        log_url = f"{GITHUB_API}/repos/{args.repo}/actions/jobs/{job_id}/logs"

        print(f"Job '{job['name']}' (id={job_id}, status={job_status})")
        try:
            download_file(log_url, token, str(out_dir / fname))
        except urllib.error.HTTPError:
            pass

    if args.skip_artifacts:
        print("\nSkipping artifacts (--skip-artifacts).")
        return

    # --- Download artifacts ---
    arts_url = f"{run_url}/artifacts"
    print(f"\nFetching artifacts...")
    artifacts = api_request_paginated(arts_url, token, opener)

    if not artifacts:
        print("  No artifacts found.")
    else:
        print(f"  Found {len(artifacts)} artifact(s)\n")
        for art in artifacts:
            art_id = art["id"]
            art_name = sanitize(art["name"])
            fname = (
                f"{repo_slug}_run{args.run_id}_art{art_id}_{art_name}.zip"
            )
            art_url = f"{GITHUB_API}/repos/{args.repo}/actions/artifacts/{art_id}/zip"

            print(f"Artifact '{art['name']}' (id={art_id})")
            try:
                download_file(art_url, token, str(out_dir / fname))
            except urllib.error.HTTPError:
                pass

    print("\nDone.")


if __name__ == "__main__":
    main()
