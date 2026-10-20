# Flake0 — classify action

Asks a model, through [OpenRouter](https://openrouter.ai), whether a failed job failed from
flakiness or from a real bug. It reads:

- the test output, saved to a file with `tee`
- the job's `collect` bundle: host metrics (PSI, CPU steal and iowait, memory, swap, disk,
  inodes, blocked processes, TCP retransmits), peak memory and CPU per tracked process, Juju
  unit status changes, and excerpts of the Juju debug and workload logs
- the pull request diff, on `pull_request` runs

Part of the **Flake0** AI debugging suite for CI tests.

## Usage

Add it as the last step of the test job. GitHub serves a job's log only after the job ends, so
the action reads the test output from a file instead:

```yaml
- name: Run tests
  shell: bash  # adds pipefail, so the step keeps the test's exit code
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
    job-name: ${{ matrix.name }}  # optional, names the matrix leg
```

Keep `shell: bash`. Without it GitHub runs `bash -e` with no `pipefail`, the step takes `tee`'s
exit code, and failing tests pass.

If the job fails before the tests start, there is no test output and the model sees only the
bundle. The diff needs `pull-requests: read` on the job.

Each call takes a few seconds and costs about $0.01 with the default model. The action never
fails the job. Without a key, or when OpenRouter is down, it logs why and exits 0.

## Inputs

| Input | Default | Description |
|---|---|---|
| `openrouter-api-key` | *(required)* | OpenRouter API key. Empty skips classification. |
| `log-path` | *(required)* | File with the test output |
| `bundle-path` | `""` | The `collect` stop phase's `bundle-path` output |
| `job-name` | `github.job` | Job name in the prompt and the summary |
| `model` | `z-ai/glm-5.3` | OpenRouter model slug |
| `github-token` | `github.token` | Token that can read the pull request diff |

## Outputs

| Output | Description |
|---|---|
| `verdict-path` | Path to `verdict.json`, empty if there is none |

## Result

A table in the job summary, and `verdict.json`:

```json
{"job": "integration (lxd)", "model": "z-ai/glm-5.3",
 "verdict": "flaky", "confidence": 0.8, "category": "resource-starvation",
 "reasoning": "...", "evidence": ["Host metrics: PSI io some peaked at 74% ..."]}
```

`verdict` is `flaky`, `real` or `inconclusive`. A reply that is not valid JSON becomes
`inconclusive`, with the text in `raw`. The prompt is in `prompt.md` next to `verdict.json`.

## Notes

### Data leaves GitHub

Test output, workload logs and diffs can hold secrets. The action sends excerpts of them to
OpenRouter and its model provider. Don't use it on repositories whose logs must stay private.

### Inputs are untrusted

Pull request code runs before this step and can write the test output and the bundle. The
action reads only regular files, so a symlink can't put this step's environment in the prompt.

### Fork pull requests

GitHub gives fork pull requests no secrets, so the action skips.

## Development

```bash
pip install ruff pytest
ruff check scripts/classify.py
shellcheck -S style scripts/*.sh

# from the repo root
pytest tests/test_classify.py

# on a real job: its test output, and the bundle from its collect artifact, extracted
OPENROUTER_API_KEY=... python3 scripts/classify.py --job "my job" --log test.log \
  --bundle bundle --out out --model z-ai/glm-5.3
```
