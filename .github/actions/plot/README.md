# Flake0 — plot action

Renders a runner-health dashboard PNG from a `collect` bundle: CPU, memory, disk, network, PSI
pressure-stall, and per-process (procstat) panels, all derived automatically from the bundle's own
tags and timestamps.

Zero configuration — there is no spec file, no phase markers, and no zoom window. `plot` renders
one dashboard, every time, spanning the bundle's full time range.

Part of the **Flake0** AI debugging suite for CI tests.

## Usage

```yaml
- uses: canonical/flake0/.github/actions/collect@main
  with: { phase: start }

- run: pytest tests/integration

- name: Stop collection
  id: collect
  uses: canonical/flake0/.github/actions/collect@main
  if: always()
  with: { phase: stop }

- name: Render dashboard
  if: always()          # plot never fails your job either way, but this
                         # keeps it running when the step above already did
  uses: canonical/flake0/.github/actions/plot@main
  with:
    bundle-path: ${{ steps.collect.outputs.bundle-path }}
    artifact-name: flake0-plot-${{ github.run_id }}
```

`plot` never fails your job: if the bundle is missing, unreadable, or produced no usable metrics,
it says so on stderr and exits 0 with an empty `health-png` output.

## Inputs

| Input | Default | Description |
|---|---|---|
| `bundle-path` | *(required)* | Path to a `collect` archive (`.tar.zst` or `.tar.gz`) |
| `artifact-name` | *(none)* | Name of the uploaded dashboard artifact; empty means don't upload |
| `retention-days` | `14` | Artifact retention |

## Outputs

| Output | Description |
|---|---|
| `health-png` | Path to the rendered dashboard PNG (empty if none was produced) |

## What you get

A single `health.png` covering the bundle's full time range:

- CPU (stacked user/system/iowait/steal, `cpu-total` only)
- PSI pressure-stall (`cpu/some`, `io/some`, `io/full`, `memory/some`)
- Memory and swap
- Root filesystem usage
- Block I/O — one read/write line pair per real disk device. Partitions and `dm-*` volumes are
  excluded; both double-count their parent whole disk.
- Network — one rx/tx line pair per real interface. Bridge interfaces (`docker0`, `lxdbr*`, `br-*`,
  `virbr*`) are excluded for the same double-counting reason. A bonded or accelerated-networking
  interface pair has no cheap way to detect as a duplicate, so you may still see two
  near-identical lines for what is really one NIC — a known limitation, not a bug.
- Load average and process counts
- Per-process memory — one line per tracked `(process_name, pid)`. `telegraf` (the collector)
  always appears here as its own overhead line; `sudo` (the collector's launcher wrapper) never
  appears in any per-process panel.
- Process lifelines — one bar per tracked process (excluding `telegraf` and `sudo`), shown only
  when `procstat` tracked something besides the collector itself.

Title and subtitle are derived from the bundle's own `repo`/`workflow`/`job`/`run_id`/`sha`/`ref`/
`runner_name` tags — every metric `collect` produces carries them.

### Running it directly

```bash
pip install -r .github/actions/plot/scripts/requirements.txt
mkdir -p /tmp/bundle && tar -xf flake0-bundle.tar.zst -C /tmp/bundle
python3 .github/actions/plot/scripts/plot_telemetry.py ./out /tmp/bundle/metrics*.json
```

## Notes

There is no first-party way to overlay test spans or job-phase markers in this repo. If you need
that, produce your own annotations and layer them on separately — this action intentionally does
not grow a configuration surface for callers that don't exist yet.

## Development

```bash
pip install -r scripts/requirements.txt ruff pytest
ruff check scripts/plot_telemetry.py
shellcheck -S style scripts/*.sh

# from the repo root
pytest tests/
```
