# Flake0 — classify action

Asks a model, through [OpenRouter](https://openrouter.ai), whether each failed job in a run
failed from flakiness or from a real bug. For every failed job it reads:

- the job log, which holds the test output (no change to how you run tests)
- the job's `collect` bundle: host metric summary (PSI, CPU steal and iowait, memory, swap,
  disk and inodes, blocked processes, TCP retransmits), peak memory and CPU of the tracked
  processes, every Juju unit status change, and Juju debug log and workload log excerpts
- the pull request diff, on `pull_request` runs

Part of the **Flake0** AI debugging suite for CI tests.

## Usage

Add one job after your test jobs. A job's log is only served once that job has finished, so
this cannot run as a step inside the test job.

```yaml
classify:
  needs: [integration]
  if: failure()
  runs-on: ubuntu-latest
  permissions:
    actions: read        # jobs, logs, artifacts
    contents: read
    pull-requests: read  # the diff
  steps:
    - uses: canonical/flake0/.github/actions/classify@main
      with:
        openrouter-api-key: ${{ secrets.OPENROUTER_API_KEY }}
```

It classifies jobs that ended with `failure` or `timed_out`. Bundles are found in this run's
artifacts whose names start with `flake0-`, which the `collect` default name does. Each bundle
is matched to its job by runner name and run attempt, so matrix legs need nothing extra. A job
without a bundle is classified from its log alone, and so is a job whose runner name has two
bundles (a reused self-hosted runner): no bundle is better than another job's.

Each failed job costs one model call, and the calls run one after another. With the default
model a call takes a few seconds and costs about $0.01, so a 60-leg matrix that fails on one
regression makes 60 calls. Give the job a `timeout-minutes` that fits your matrix.

`classify` never fails your job. Without a key, without failed jobs, or when OpenRouter is
unreachable, it says so and exits 0.

## Inputs

| Input | Default | Description |
|---|---|---|
| `openrouter-api-key` | *(required)* | OpenRouter API key. Empty skips classification. |
| `model` | `z-ai/glm-5.3` | OpenRouter model slug |
| `github-token` | `github.token` | Token that can read the run's jobs, logs, artifacts and pull request |

## Outputs

| Output | Description |
|---|---|
| `verdicts-path` | Path to `verdicts.json` (empty if none was produced) |

## What you get

A table in the job summary, and `verdicts.json` with one entry per failed job:

```json
{"job": "integration (lxd)", "job_id": 1, "model": "z-ai/glm-5.3",
 "verdict": "flaky", "confidence": 0.8, "category": "resource-starvation",
 "reasoning": "...", "evidence": ["Host metrics: PSI io some peaked at 74% ..."]}
```

`verdict` is `flaky`, `real` or `inconclusive`. An answer that is not valid JSON becomes
`inconclusive` with the text kept in `raw`. The prompt sent for each job sits next to
`verdicts.json` as `prompt-<job id>.md`.

## Notes

### Data leaves GitHub

Job logs, workload logs and diffs can hold secrets, and this action sends excerpts of them to
OpenRouter and the model provider behind it. Do not use it on repositories whose logs must stay
private.

### Bundles are untrusted

A bundle comes from the test job, which can run pull request code. Only regular files are kept
after extraction, so a symlink in a bundle cannot pull this job's environment or files into the
prompt.

### `gh` version

Job logs are fetched with `gh api --allow-escape-sequences`, which older `gh` releases lack.
GitHub-hosted runners have it. On a self-hosted runner with an older `gh`, every job is
classified without its log.

### Fork pull requests

GitHub gives no secrets to workflows from forks, so `classify` skips on them.

## Development

```bash
pip install ruff pytest
ruff check scripts/classify.py
shellcheck -S style scripts/*.sh

# from the repo root
pytest tests/test_classify.py

# against a real run: fetch a job log, then classify it
gh api --allow-escape-sequences repos/OWNER/REPO/actions/jobs/JOB_ID/logs > logs/JOB_ID.log
echo '{"id": JOB_ID, "name": "my job", "runner_name": "..."}' > jobs.ndjson
OPENROUTER_API_KEY=... python3 scripts/classify.py --jobs jobs.ndjson --logs logs \
  --bundles bundles --out out --model z-ai/glm-5.3
```
