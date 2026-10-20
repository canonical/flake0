# Flake0 — classify action

Asks a model, through [OpenRouter](https://openrouter.ai), whether a failed job failed from
flakiness or from a real bug. It reads:

- the test output, which you save to a file with `tee`
- the job's `collect` bundle: host metric summary (PSI, CPU steal and iowait, memory, swap,
  disk and inodes, blocked processes, TCP retransmits), peak memory and CPU of the tracked
  processes, every Juju unit status change, and Juju debug log and workload log excerpts
- the pull request diff, on `pull_request` runs

Part of the **Flake0** AI debugging suite for CI tests.

## Usage

Add it as the last step of your test job. GitHub serves a job's own log only once the job has
finished, so save the test output with `tee` and pass the file:

```yaml
- name: Run tests
  shell: bash  # pipefail, so the tee keeps the test's exit code
  run: tox -e integration 2>&1 | tee "$RUNNER_TEMP/test.log"
- name: Stop telemetry
  id: collect
  if: always()
  uses: canonical/flake0/.github/actions/collect@main
  with:
    phase: stop
- name: Classify failure
  if: failure()
  timeout-minutes: 5
  uses: canonical/flake0/.github/actions/classify@main
  with:
    openrouter-api-key: ${{ secrets.OPENROUTER_API_KEY }}
    log-path: ${{ runner.temp }}/test.log
    bundle-path: ${{ steps.collect.outputs.bundle-path }}
    job-name: ${{ matrix.name }}  # optional, tells matrix legs apart
```

Without `shell: bash`, GitHub runs `bash -e` without `pipefail`, and the pipe takes `tee`'s exit
code: the test step would pass when the tests fail.

A step that fails before the tests run (a setup timeout, a snap download) leaves no test output:
the model then has the bundle alone.

The diff needs `pull-requests: read` on the job.

Each failed job makes one model call. With the default model a call takes a few seconds and
costs about $0.01.

`classify` never fails your job. Without a key, or when OpenRouter is unreachable, it says so
and exits 0.

## Inputs

| Input | Default | Description |
|---|---|---|
| `openrouter-api-key` | *(required)* | OpenRouter API key. Empty skips classification. |
| `log-path` | *(required)* | File with the test output |
| `bundle-path` | `""` | The `collect` stop phase's `bundle-path` output |
| `job-name` | `github.job` | Name shown in the prompt and the summary |
| `model` | `z-ai/glm-5.3` | OpenRouter model slug |
| `github-token` | `github.token` | Token that can read the pull request diff |

## Outputs

| Output | Description |
|---|---|
| `verdict-path` | Path to `verdict.json` (empty if none was produced) |

## What you get

A table in the job summary, and `verdict.json`:

```json
{"job": "integration (lxd)", "model": "z-ai/glm-5.3",
 "verdict": "flaky", "confidence": 0.8, "category": "resource-starvation",
 "reasoning": "...", "evidence": ["Host metrics: PSI io some peaked at 74% ..."]}
```

`verdict` is `flaky`, `real` or `inconclusive`. An answer that is not valid JSON becomes
`inconclusive` with the text kept in `raw`. The prompt sits next to `verdict.json` as
`prompt.md`.

## Notes

### Data leaves GitHub

Test output, workload logs and diffs can hold secrets, and this action sends excerpts of them to
OpenRouter and the model provider behind it. Do not use it on repositories whose logs must stay
private.

### Inputs are untrusted

The test output and the bundle come from steps that can run pull request code. Only regular
files are read, so a symlink cannot pull this step's environment or files into the prompt.

### Fork pull requests

GitHub gives no secrets to workflows from forks, so `classify` skips on them.

## Development

```bash
pip install ruff pytest
ruff check scripts/classify.py
shellcheck -S style scripts/*.sh

# from the repo root
pytest tests/test_classify.py

# against a real job: a test output, and the bundle extracted from its collect artifact
OPENROUTER_API_KEY=... python3 scripts/classify.py --job "my job" --log test.log \
  --bundle bundle --out out --model z-ai/glm-5.3
```
