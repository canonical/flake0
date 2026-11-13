# Flake0 — classify action

Runs the [opencode](https://opencode.ai) agent `flaky-test-investigator`
(`.opencode/agents/`) on a failed job, through [OpenRouter](https://openrouter.ai). The agent
investigates the job's own logs and metrics, then classifies the failure in the flake0
taxonomy (`docs/taxonomy.md`).

Part of the **Flake0** AI debugging suite for CI tests.

## Usage

Add it as the last step of the test job, after `collect`'s stop phase:

```yaml
permissions:
  contents: read
  actions: read        # run history
  pull-requests: read  # the diff
steps:
  # ...
  - name: Run tests
    shell: bash  # adds pipefail, so the step keeps the test's exit code
    run: tox -e integration 2>&1 | tee "$RUNNER_TEMP/test.log"
  - name: Stop telemetry
    id: collect
    if: always()
    uses: canonical/flake0/.github/actions/collect@main
    with:
      phase: stop
      test-log: ${{ runner.temp }}/test.log
  - name: Classify failure
    if: failure()
    timeout-minutes: 25
    uses: canonical/flake0/.github/actions/classify@main
    with:
      openrouter-api-key: ${{ secrets.OPENROUTER_API_KEY }}
      bundle-path: ${{ steps.collect.outputs.bundle-path }}
      job-name: ${{ matrix.name }}  # optional, names the matrix leg
```

The action never fails the job. Without a key it logs why and exits 0. When opencode fails,
times out or gives no verdict, the verdict is `INCONCLUSIVE` with an `error`.

### Time and cost

The agent gets 45 steps (`opencode.json`) and 20 minutes. The cap is sized to hit first: at the
cap opencode turns tools off and the agent answers in text, and the action takes the verdict from
that last message. When a run ends without a verdict (provider error, timeout or cap), the action
asks once more in the same session, for up to 3 minutes. Keep `timeout-minutes` at 25, above the
20 + 3.

Every step sends the whole context again, so the cost grows faster than the step count. The
default model was right on four real failures (valkey-operator and charmed-etcd-operator) in
8-30 steps, 2.6-5.1 minutes and $0.04-0.09 each. Other models cost from 10x to 200x more on the
same jobs. Cap the spend with a credit limit on the OpenRouter key: it covers every job and
repository that uses the key.

## Inputs

| Input | Default | Description |
|---|---|---|
| `openrouter-api-key` | *(required)* | OpenRouter API key. Empty skips classification. |
| `bundle-path` | `""` | The `collect` stop phase's `bundle-path` output |
| `job-name` | `github.job` | Job name in the prompt and the summary |
| `model` | `deepseek/deepseek-v4.1-flash` | OpenRouter model slug |
| `github-token` | `github.token` | Token for run history and the diff. Keep it read-only. |

## Outputs

| Output | Description |
|---|---|
| `verdict-path` | Path to `verdict.json`, empty if classification was skipped |

## Result

A table in the job summary, the verdict in the step log, and `verdict.json`:

```json
{"job": "integration (lxd)", "model": "deepseek/deepseek-v4.1-flash",
 "verdict": "FLAKY", "root_cause": "environment_infra", "recommendation": "retry_safe",
 "confidence_pct": 80, "confidence_bucket": "high",
 "failed_tests": ["test_cache_warmup_meets_deadline"], "summary": "...", "suggested_fix": "",
 "error_excerpt": "...", "reasoning": "...", "evidence": ["metrics.ndjson: PSI io some at 74% ..."]}
```

The agent fills in every field except `job` and `model`, and the action doesn't check them. It
writes `verdict.json`, or, at the steps cap, puts the same object in its last message. The
[`summarize`](../summarize/README.md) action shows only known labels in the pull request
comment. The agent's session is in `session.ndjson` next to `verdict.json`.

## What the agent sees

The agent runs in a new directory that holds:

- `bundle/`: this job's `collect` bundle, with `metrics.json` rewritten to `metrics.ndjson`, one
  metric per line. opencode's read and grep tools cut lines at about 2000 characters, and
  Telegraf writes a whole batch per line.
- `src/`: the repository at the tested commit, from `git archive HEAD`
- `docs/taxonomy.md`

It reads them with opencode's read, glob and grep tools. Its shell runs read-only `gh` only, for
run history and the diff. It can write `verdict.json` and nothing else.

## Notes

### Data leaves GitHub

Test output, workload logs, source and diffs can hold secrets. The agent sends what it reads to
OpenRouter and its model provider. Don't use the action on repositories whose logs must stay
private.

### Inputs are untrusted

Pull request code runs earlier in the job and can write the bundle, the workspace, the
environment and `PATH`. The action:

- deletes symlinks, FIFOs and other non-regular files, since the read tool follows symlinks
- runs `git archive` without the secrets in its environment, since `.git/config` can run filters
- runs opencode under `env -i` with `OPENCODE_DISABLE_PROJECT_CONFIG=1`, its own config and an
  empty `HOME`, so no `opencode.json`, `.opencode/` or `AGENTS.md` that PR code wrote is loaded
- installs a pinned opencode with a checked SHA256 and calls it by path
- denies every tool but read, glob, grep, skill, `gh` reads and writing `verdict.json`.
  `external_directory` keeps the read tools inside the directory.

Limits it can't fix from inside the job:

- A process left running by PR code, as the same user, can read the key and the token from
  opencode's `/proc/<pid>/environ`. Root PR code, with passwordless `sudo`, can do anything.
- The `gh` rules are a deny-list (no writes, redirects, `--jq` or other hosts). A gap there lets a
  prompt injected through the logs reach the token.

### Fork pull requests

GitHub gives fork pull requests no secrets, so the action skips.

## Development

```bash
# from the repo root
pytest tests/test_classify.py   # run.sh, with tests/fakes/opencode
shellcheck -S style .github/actions/classify/scripts/*.sh
ruff check .github/actions/classify/scripts/final_verdict.py

# on a real job: its collect bundle, with opencode on PATH
OPENROUTER_API_KEY=... OPENCODE_BIN="$(command -v opencode)" BUNDLE_PATH=bundle.tar.zst \
  GITHUB_REPOSITORY=owner/repo GITHUB_RUN_ID=1 GITHUB_WORKSPACE="$PWD" JOB_NAME="my job" \
  .github/actions/classify/scripts/run.sh
```
