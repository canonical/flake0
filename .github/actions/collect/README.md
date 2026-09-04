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
| `artifact-name` | `flake0-collect-<run_id>-<attempt>-<job>` | Name of the uploaded artifact |
| `retention-days` | `14` | Artifact retention |
| `collect-dir` | `$RUNNER_TEMP/flake0-collect` | Collection directory |
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

Collected: `cpu` (per-core, incl. `usage_steal`), `mem`, `swap`, `pressure` (PSI — cpu/mem/io
stall time, the highest-signal metric here), `disk` (bytes **and inodes**), `diskio`, `net`,
`netstat`, `nstat`, `processes`, `kernel`, `linux_sysctl_fs`, `conntrack` (when the module is
loaded), and `procstat` for jujud, containerd, dockerd, lxd, snapd, kubelet, pebble, mongod,
pytest and Telegraf itself.

Every metric carries `run_id`, `run_attempt`, `repo`, `workflow`, `job`, `sha`, `ref`,
`runner_name`, `runner_os` and `image_os`, so you can pull up one run or diff a good run against
a flaky one.

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

Telegraf is launched under `sudo` when passwordless `sudo` is available, for a handful of
root-only `/proc` reads. Without it, everything still runs unprivileged and only those few
metrics are missing.

## Development

```bash
shellcheck -S style scripts/*.sh

# run the collector locally; tags fall back to "local" off-runner
RUNNER_TEMP=/tmp/t ./scripts/start.sh
RUNNER_TEMP=/tmp/t ./scripts/stop.sh
```

CI (`.github/workflows/smoke-collect.yml`) runs shellcheck, a full start/load/stop cycle on
`ubuntu-22.04`, `ubuntu-24.04` and `ubuntu-24.04-arm` with bundle and overhead assertions, and a
regression job that puts shell metacharacters in every tag value.
