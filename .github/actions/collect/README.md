# Flake0 — collect action

Lightweight host telemetry for CI runners. Records what the machine was doing while your tests
ran, and uploads it as a workflow artifact — including (especially) when the job fails.

Integration tests that fail or flake on hosted runners usually leave nothing behind but the test
log, which cannot tell you that the CPU was starved, memory ran out, inodes filled up, I/O
stalled past a `wait-for` timeout, or snapd refreshed mid-run. This action records that context
at 2-second resolution so you can go back and look.

Part of the **Flake0** AI debugging suite for CI tests.

Linux `x86_64` / `aarch64` runners only.

## Usage

```yaml
- uses: canonical/flake0/.github/actions/collect@main
  with: { phase: start }

- run: pytest tests/integration

- uses: canonical/flake0/.github/actions/collect@main
  if: always()          # the failing runs are the ones worth capturing
  with: { phase: stop }
```

`stop` never fails your job: if the collector never started, or died mid-run, it says so and
exits 0.

## Inputs

| Input | Default | Description |
|---|---|---|
| `phase` | *(required)* | `start` or `stop` |
| `telegraf-version` | `1.39.2` | Telegraf to install. See [Version pinning](#version-pinning). |
| `artifact-name` | `flake0-collect-<run_id>-<attempt>-<job>` | Name of the uploaded artifact. Set it in a matrix, because the default repeats across legs and `upload-artifact@v4` rejects duplicates. `stop` reuses the name given at `start`. |
| `retention-days` | `14` | Artifact retention |
| `collect-dir` | `$RUNNER_TEMP/flake0-collect` | Collection directory |
| `logs` | `true` | Follow Juju, workload, kernel and snapd logs and the job log. `false` skips them. `pytest.log` comes only from `test-log`, and test markers only from `markers`. |
| `redact-values` | `""` | Path to a file of secret values (one per line) to mask in collected logs before bundling. See [Workload logs](#workload-logs). |
| `markers` | `false` | `start` only. Load the test markers pytest plugin in later steps. See [Test output and markers](#test-output-and-markers). |
| `test-log` | `""` | `stop` only. Path to the job's test output, copied into the bundle as `pytest.log`. See [Test output and markers](#test-output-and-markers). |
| `cache-telegraf` | `true` | Cache the binary between jobs, avoiding an ~82 MB download per run |

## Outputs

| Output | Description |
|---|---|
| `bundle-path` | Path to the bundle written by `stop` (empty if none was produced) |
| `artifact-name` | Name of the uploaded artifact |

## What you get

A `tar.zst` artifact containing:

| File | Contents |
|---|---|
| `metrics.json` | Telegraf batches, one JSON object per line: `{"metrics": [...]}` |
| `telegraf.log` | Collector log |
| `telegraf.resolved.conf` | The exact config used, for provenance |
| `juju-debug-<store>-<controller>-<model>.log` | Juju model log with `# flake0:` markers, when Juju is used |
| `juju-status-<store>-<controller>-<model>.ndjson` | `{"ts": ..., "status": <juju status JSON>}` every 30 s for workload models, when Juju is used |
| `watcher.log` | Juju watcher diagnostics, when Juju is used |
| `workload/k8s/<pod>_<ns>_<container>-<id>.log` | Pod container stdout, one file per container instance, when Kubernetes is used |
| `workload/lxd/<container>/journal.log` | LXD container journal, when LXD is used |
| `workload/lxd/<container>/<snap>_<dir>_<file>` | Snap log files under `/var/snap/<snap>/common/var/log/<dir>/` in LXD containers |
| `workload-watcher.log` | Workload watcher diagnostics, when something was followed |
| `kernel.log` | New kernel messages: OOM kills, AppArmor denials, I/O errors. Needs root or passwordless `sudo` |
| `snapd.log` | New lines of snapd's journal. Needs root or passwordless `sudo` |
| `host-watcher.log` | Errors from the two followers above, usually empty |
| `env-start.json`, `env-end.json` | Kernel version, snaps and snapd changes at `start` and `stop` |
| `pytest.log` | The file named by `test-log`, when set |
| `job.log` | The job's own log, from Set up job up to the `stop` step. See [Job log](#job-log) |
| `markers.ndjson` | One start and one finish line per test, with `markers: true` |

Collected: `cpu` (per-core, incl. `usage_steal`), `mem`, `swap`, `pressure` (PSI — cpu/mem/io
stall time, the highest-signal metric here), `disk` (bytes **and inodes**), `diskio`, `net`,
`netstat`, `nstat`, `processes`, `kernel`, `linux_sysctl_fs`, `conntrack` (when the module is
loaded), and `procstat` for jujud, containerd, dockerd, lxd, snapd, kubelet, pebble, mongod,
pytest, Telegraf itself, and the log watchers with their `juju`, `lxc` and `tail` processes and
the `journalctl` they start inside LXD containers.

Every metric carries `run_id`, `run_attempt`, `repo`, `workflow`, `job`, `sha`, `ref`,
`runner_name`, `runner_os` and `image_os`, so you can pull up one run or diff a good run against
a flaky one.

### Juju logs

The action records Juju without configuration, even when Juju is installed after `start`. Every
30 s it looks for controllers in the runner user's store (`$JUJU_DATA` or `~/.local/share/juju`)
and, with passwordless `sudo`, in root's. Spread runs tests as root, so most charm CI uses root's
store. `<store>` in file names is `user` or `root`.

Every model gets its own follower, including models a test creates and destroys. A follower that
drops reconnects with `--replay`, which re-sends history. Markers bound the duplicates:

```
# flake0: connect root:concierge-lxd:testing at 2026-09-25T10:00:00.000Z
# flake0: reconnect 1 at 2026-09-25T10:05:00.100Z after exit 1
# flake0: capped at ...     (over 50 MB, reconnects stop replaying)
# flake0: ended at ...      (the model is gone)
```

Stores of other users and a `JUJU_DATA` set inside a test step are not covered. Each followed model
adds a `juju` process with about 25 MB of private memory.

### Workload logs

With root or passwordless `sudo`, the action also follows workload logs while units run, so a unit
that a test removes keeps its logs. Every 15 s it looks for:

- pod containers, through the kubelet's `/var/log/containers`. A restarted container is a new
  instance, so it gets a new file. Valkey's K8s charm and OpenSearch send their logs to stdout.
- running LXD containers in every project, through LXD's `[lxc monitor]` processes. It follows each
  container's journal with `lxc exec ... journalctl -f`, and each
  `/var/snap/<snap>/common/var/log/<dir>/*.log` file, the layout of the Data Platform snaps.

The watcher runs as root, so it needs root or passwordless `sudo`. It never runs `lxc` before a
container exists, so it never installs or wakes LXD. Each file starts with
`# flake0: connect <source> at <ts>`. A restarted container or a dropped journal replays from the
top, after a new marker.

Pod output and container journals land in the artifact, so anything a workload prints could become
readable by anyone who can read the repo. Before bundling, `stop` masks secrets in every collected
log (see [Redaction](#redaction)), but this is best-effort. When logs may carry sensitive data you
cannot enumerate, set `logs: false` and leave `test-log` and `markers` unset.

#### Redaction

The `stop` phase rewrites every `*.log` file and everything under `workload/` in place before they
are bundled, masking:

- every value listed in the `redact-values` file (one per line) — the reliable layer; point it at the
  job's known credentials, e.g. values you would otherwise pass to `add-mask`.
- common secret formats (`password=`, `token:`, auth schemes, URL credentials, JWTs, AWS keys, PEM
  private-key blocks) via `scripts/redact.sed` — best-effort, so it will miss custom, encoded or split secrets.
  A private-key block with no END line masks the rest of that file.

Logs are unredacted on the runner's disk while the job runs and only masked at `stop`, so treat the
runner as trusted; the exposure this closes is the shared artifact. The Juju `*-status.ndjson` files
are left untouched to keep them valid JSON.

`markers.ndjson` is masked too. pytest puts parameter values in test ids, so
`test_login[password=hunter2]` lands in the file. Masking only catches the formats above and your
`redact-values`, so `test_login[hunter2]` stays readable unless `hunter2` is listed. A listed value
with a quote, a backslash or a non-ASCII character does not match either, because pytest and JSON
escape it in the id. Don't parametrize tests with secrets, or give those parameters an `ids=` value.
A failed test's `failure` text holds whatever the assertion or exception printed, masked the same way.
The private-key rule needs the key's lines as separate lines, so a key inside `failure` is not masked.

> [!WARNING]
> The `env-*.json` snapshots are not redacted, and `logs: false` does not turn them off. `changes`
> includes the `err` of a failed snapd change and the `log` of its tasks, which can hold snap hook
> output. Anyone who can read the artifact can read those fields.

#### Memory footprint

Each followed file costs one `tail` process, and coreutils differ a lot between runner images. PSS
measured by procstat on the K8s legs of `consumer-sim`, which followed 31 files, 19 of them from
`kube-system` and `metallb`:

| Runner | `tail` | Per file | 31 files |
|---|---|---|---|
| `ubuntu-24.04` | GNU | about 0.4 MB | 12 MB |
| `ubuntu-26.04` | uutils 0.10.0 | about 1.7 MB | 53 MB |
| `ubuntu-26.04-arm` | uutils 0.10.0 | about 3.9 MB | 122 MB |

Each LXD container adds an `lxc exec` client of about 22 MB RSS for its journal. An LXD run follows
2 or 3 containers and a few snap log files. Every run reports these numbers in `metrics.json`,
because procstat watches the watchers and their followers.

### Kernel, snapd and environment

With root or passwordless `sudo`, the action follows `dmesg -W` into `kernel.log` and
`journalctl -u snapd -f` into `snapd.log`. Both start at `start` and hold only new lines. The kernel
log is not filtered, because an OOM report spans many lines.

`env-start.json` and `env-end.json` come from snapd's REST API and need no root:

```json
{"ts": "2026-11-06T10:00:00.123Z", "kernel": "6.8.0-1020-azure", "snaps": [...], "changes": [...]}
```

`changes` lists snapd's recent changes with their times, about the last day, so a refresh just
before the job shows up in `env-start.json`. Compare the `revision` of each snap in the two files to catch a refresh mid-run.
`snaps` and `changes` are `null` without snapd. These files are not redacted, see
[Redaction](#redaction).

### Job log

`job.log` is the job's log as the Actions UI shows it: every step from Set up job and Checkout up
to the `stop` step, with each `##[error]`. A step that runs after `stop`, such as a classifier, can
read the whole job from the bundle. It needs no root and no token.

The runner writes this log to `_diag/pages` under its own directory, in pages of about 8 MB, and
deletes each page after it uploads it. A watcher hard-links each page into the collect dir as it
appears, and `stop` joins them. These are undocumented runner internals, tested on runner 2.338.2.
If they change, `job.log` is missing and `stop` prints a warning. Secrets are masked as GitHub
masks them in its own log, and the [Redaction](#redaction) rules run on top.

### Test output and markers

`stop` copies the file named by `test-log` into the bundle as `pytest.log` and redacts it. Use
`shell: bash`, which sets `pipefail`, so `tee` keeps the test exit code:

```yaml
- shell: bash
  run: pytest tests/integration 2>&1 | tee "$RUNNER_TEMP/test.log"

- uses: canonical/flake0/.github/actions/collect@main
  if: always()
  with:
    phase: stop
    test-log: ${{ runner.temp }}/test.log
```

`markers.ndjson` records when each test started and finished, so you can match log lines to a test:

```json
{"ts":"2026-11-06T10:00:00.123Z","event":"start","nodeid":"tests/integration/test_x.py::test_y"}
{"ts":"2026-11-06T10:00:12.456Z","event":"finish","nodeid":"tests/integration/test_x.py::test_y","outcome":"failed","duration":12.3,"failure":"...\nE       assert 1 == 2\n\ntests/integration/test_x.py:42: AssertionError"}
```

`outcome` is `passed`, `skipped`, `failed`, or `error` when setup or teardown failed. A failed test's
finish line also has `failure`: the last 4,000 characters of pytest's report for the first phase that
failed, where the exception and the assertion are. An attempt that
`pytest-rerunfailures` retries counts as failed, and each attempt gets its own pair of lines.
`pytest-retry` retries inside one pair, so a test that passes on retry still shows `failed`.

Turn it on at `start`:

```yaml
- uses: canonical/flake0/.github/actions/collect@main
  with:
    phase: start
    markers: true
```

`start` then writes `FLAKE0_MARKERS`, `PYTEST_PLUGINS` and `PYTHONPATH` to `$GITHUB_ENV`, adding to
any `PYTEST_PLUGINS` or `PYTHONPATH` set before it. A pytest in a later step needs nothing else. A
step that sets its own `PYTHONPATH` must keep the existing value, because pytest stops at startup if
it cannot import the plugin.

Spread and tox drop the environment, so a charm repo forwards it through both:

```yaml
# spread.yaml, github-ci backend
environment:
  FLAKE0_MARKERS: "$(HOST: echo $FLAKE0_MARKERS)"
  PYTEST_PLUGINS: "$(HOST: echo $PYTEST_PLUGINS)"
  PYTHONPATH: "$(HOST: echo $PYTHONPATH)"
```

```ini
# tox.ini
[testenv]
set_env =
    # add :{env:PYTHONPATH:} to the existing line
    PYTHONPATH = {[vars]src_path}:{tox_root}/lib:{env:PYTHONPATH:}

[testenv:integration]
pass_env =
    FLAKE0_MARKERS
    PYTEST_PLUGINS
```

Make all of these changes together. `set_env` overrides `pass_env`, so `PYTHONPATH` has to be merged
there, and `PYTEST_PLUGINS` without the plugin on `PYTHONPATH` makes pytest fail at startup, before any
test runs.

### Reading a bundle

```bash
gh run download <run-id> -n flake0-collect-<run-id>-1-<job>
tar -xf flake0-bundle.tar.zst

# what stalled, and for how long (PSI stall % and cumulative µs stalled)
jq -r '.metrics[]|select(.name=="pressure" and .tags.type=="some")
       |"\(.timestamp) \(.tags.resource) avg10=\(.fields.avg10)% total=\(.fields.total // 0)us"' metrics.json

# CPU steal — someone else's workload on your hypervisor
jq -r '.metrics[]|select(.name=="cpu" and .tags.cpu=="cpu-total")
       |"\(.timestamp) \(.fields.usage_steal)"' metrics.json

# inodes remaining, the failure mode that looks like nothing else
jq -r '.metrics[]|select(.name=="disk")|"\(.tags.path) \(.fields.inodes_free)"' metrics.json
```

`metrics.json` is one JSON document per line. When asserting over it, note that
`jq -e '<cond>' metrics.json` reflects only the **last** line — use
`jq -ne 'all(inputs; <cond>)'` instead.

## Overhead

Measured on every run, by having Telegraf watch its own process; the numbers travel inside the
bundle and CI fails if they regress. On a 28-core host with 31 watched processes:

| | Measured | CI gate |
|---|---|---|
| CPU | 2.5% of one core (0.09% of the machine) | ≤ 10% of one core |
| Memory | 44 MB private (63 MB Go `sys_bytes`) | ≤ 150 MB `sys_bytes` |
| Disk | ~33 MB per 30 min, ~18x smaller once packed | — |

Expect less on a smaller runner: cost scales with core count and with the number of matched
processes. Expensive `/proc` walks are sampled at 10 s and 30 s rather than 2 s precisely to keep
this in range — see [`docs/plan.md` §7](docs/plan.md) for the per-input cost table and the
reasoning.

## Notes

### Command lines are deliberately not recorded

Telegraf's `procstat` input records each process's full command line by default. Since the bundle
is downloadable by anyone who can read the repo, that would publish any secret passed in `argv`.
`telegraf.conf` drops the field, and a smoke-test assertion keeps it dropped.

### Version pinning

The SHA256 of the default Telegraf version is committed in `scripts/start.sh`, so a
tampered upstream tarball fails the check. Overriding `telegraf-version` falls back to the
`.DIGESTS` file published alongside the tarball — that verifies integrity but not provenance,
and the action warns when it happens. Prefer the default.

### Root

Telegraf and the workload and host log watchers are launched under `sudo` when passwordless `sudo`
is available. Telegraf needs it for a handful of root-only `/proc` reads, and every workload log,
the kernel log and snapd's journal are root-only. Without it, Telegraf runs unprivileged with those
few metrics missing, the Juju watcher sees only the runner user's client store, and no workload
logs, `kernel.log` or `snapd.log` are collected.

## Development

```bash
shellcheck -S style scripts/*.sh

# run the collector locally; tags fall back to "local" off-runner
RUNNER_TEMP=/tmp/t ./scripts/start.sh
RUNNER_TEMP=/tmp/t ./scripts/stop.sh
```

CI (`.github/workflows/smoke-collect.yml`) runs shellcheck, the unit tests, a full
start/load/stop cycle on `ubuntu-24.04` and `ubuntu-24.04-arm` with bundle and overhead
assertions, and a regression job that puts shell metacharacters in every tag value.
`.github/workflows/consumer-sim.yml` reproduces charm CI (spread running tests as root, concierge
bootstrapping Juju after `start`) to test the log watchers against real controllers, LXD and
Canonical K8s.
