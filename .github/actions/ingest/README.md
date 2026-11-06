# Flake0 — ingest action

Loads a `collect` bundle into OpenSearch 2.x, so you can search one run's metrics, logs and tests
together, or compare a flaky run with a good one. It runs after `collect` stops, in CI or on your
machine against a downloaded bundle. Nothing streams during the job.

Part of the **Flake0** AI debugging suite for CI tests.

## Usage

```yaml
- name: Stop collection
  id: collect
  uses: canonical/flake0/.github/actions/collect@main
  if: always()
  with: { phase: stop }

- name: Load into OpenSearch
  if: always()
  uses: canonical/flake0/.github/actions/ingest@main
  with:
    bundle-path: ${{ steps.collect.outputs.bundle-path }}
    url: ${{ secrets.OPENSEARCH_URL }}
    username: ${{ secrets.OPENSEARCH_USER }}
    password: ${{ secrets.OPENSEARCH_PASSWORD }}
    ca-cert: ${{ secrets.OPENSEARCH_CA }}
```

`ingest` never fails your job. If loading fails it prints the reason and a warning. With an empty
`url`, as on pull requests from forks, which get no secrets, it skips with a notice.

On your machine, the same script takes an archive or an extracted bundle. Settings come from the
environment, so the password never shows in the process list. It exits non-zero on failure:

```bash
gh run download <run-id> -n flake0-collect-<run-id>-1-<job>
export OPENSEARCH_URL=https://opensearch.example.com:9200 OPENSEARCH_USER=flake0
read -rs OPENSEARCH_PASSWORD && export OPENSEARCH_PASSWORD
export OPENSEARCH_CA=~/opensearch-ca.pem   # only for a private CA
python3 .github/actions/ingest/scripts/ingest.py flake0-bundle.tar.zst
```

It needs Python 3.8 or later and nothing outside the standard library. `tar` needs `zstd` for a
`.tar.zst` bundle.

## Inputs

| Input | Default | Description |
|---|---|---|
| `bundle-path` | *(required)* | Path to a `collect` archive (`.tar.zst` or `.tar.gz`) |
| `url` | `""` | OpenSearch URL. Only `https://` is accepted. Empty skips loading |
| `username` | `""` | OpenSearch user |
| `password` | `""` | OpenSearch password |
| `ca-cert` | `""` | PEM text of the CA that signed the cluster's certificate, when it is not a public one |

TLS is always verified. There is no option to turn that off.

## What lands where

| Index | One document per | From |
|---|---|---|
| `ci-metrics-YYYY.MM` | Telegraf metric | `metrics*.json` |
| `ci-flake0-metrics-YYYY.MM` | Telegraf's `internal_*` metric, about the collector itself | `metrics*.json` |
| `ci-logs-<source>-YYYY.MM` | non-empty log line | every `*.log` file, one index per source below |
| `ci-meta` | run, and test attempt | `env-start.json`, `env-end.json`, `markers.ndjson` |
| `ci-juju-status-YYYY.MM` | Juju status snapshot | `juju-status-*.ndjson` |
| `ci-flake0-logs-YYYY.MM` | non-empty line of the collector's own logs | `telegraf.log`, `*watcher.log` |

The month comes from each document's own time. `telegraf.resolved.conf` is not loaded.

Each log format has its own index, and `ci-logs-*` searches all of them. Each source parses its
own fields out of the line:

| Source | Files | Parsed fields |
|---|---|---|
| `juju` | `juju-debug-*.log` | `controller`, `model` (from the connect marker), `entity` (`unit-valkey-0`), `level`, `module` |
| `k8s` | `workload/k8s/*.log`, pod container output | `pod`, `namespace`, `container` (from the file name), `stream`, `service` (pebble's `[name]`, such as `container-agent`) |
| `lxd` | `workload/lxd/<container>/journal.log` | `container`, `host`, `process`, `pid` |
| `app` | the other files under `workload/lxd/`, the snaps' own log files | `container`, `snap`, `log` (from the path) |
| `host` | `kernel.log`, `snapd.log` | snapd lines: `host`, `process`, `pid` |
| `job` | `job.log` | `step`, from the last `##[group]Run <step>` line |
| `pytest` | `pytest.log` | none |

Every document carries `bundle_id`, which tells matrix legs and repeated jobs apart. Logs, meta and
Juju status documents also carry the run tags as `tag.run_id`, `tag.repo`, `tag.job` and so on, the same field
names the metrics use. The tags come from `metrics.json`. If the collector never wrote a metric,
they come from the job's `GITHUB_*` variables, or are `local`.

- **Metrics** have the shape Telegraf's own `outputs.opensearch` writes: `@timestamp`,
  `measurement_name`, `tag.*` and `<measurement>.<field>`, for example `pressure.avg10`. Live
  streaming can then share these indices later. Every number is mapped as `double`. The collector's
  own `internal_*` measurements go to `ci-flake0-metrics-*`, so `ci-metrics-*` holds only the
  runner's metrics.
- **Logs** have `file`, `line`, `message`, `@timestamp` and their source's fields. `message` holds
  what follows the parsed prefix: `boom` for `unit-valkey-0: 2026-11-06 10:00:01.000 ERROR
  unit.valkey/0.juju-log boom`. A line without a time of its own, such as a traceback line, takes
  the time and fields of the line before it, so `level: ERROR` finds the whole traceback. A line with no time before it, such as
  most of `pytest.log`, has no `@timestamp` and doesn't show up in time-filtered views. Search those
  by `bundle_id`.
- **Meta** has one `type: run` document per bundle, with `env_start` and `env_end`, and one
  `type: test` document per test attempt, with `nodeid`, `attempt`, `outcome`, `@timestamp` (start),
  `end`, `duration`, and for a failed attempt `failure`, the end of pytest's report as searchable
  text. A test that never finished, for example because the job timed out, has
  `outcome: unfinished` and ends when the job did. Snap lists and snapd changes are stored but not
  searchable.
- **Juju status** has one document per 30 s snapshot, with `controller`, `model`
  and a `units` list of `app`, `unit`, `workload_status`, `agent_status` and `message`, subordinates
  included. The full `juju status` output is in `status`, stored but not searchable. `units` is a
  plain list, so a search for one unit and one status matches a snapshot where any unit has that
  status. Like the bundle's copy, it is not redacted. Juju retries a failed hook within seconds, so
  a unit can be in `error` between two snapshots and never show it. Search the Juju log for hook
  failures, as below.

Loading a bundle again overwrites its documents and does not duplicate them. When a Juju or
workload follower reconnects, it replays history that is already in the file, after a `# flake0:`
line. Each replayed line becomes the same document as the original, so it is counted once. A line
that really repeats, even within one timestamp, keeps its own document.

## Searching

AppArmor denials, in Dashboards' Discover:

```
message:"apparmor DENIED"
```

PSI while a test was failing. First get the failed attempt's window:

```json
GET ci-meta/_search
{"query": {"bool": {"filter": [
  {"term": {"outcome": "failed"}},
  {"term": {"nodeid": "tests/integration/test_x.py::test_y"}}]}}}
```

Then search pressure in that window, with its `bundle_id`, `@timestamp` and `end`:

```json
GET ci-metrics-*/_search
{"sort": ["@timestamp"], "query": {"bool": {"filter": [
  {"term": {"bundle_id": "<bundle_id>"}},
  {"term": {"measurement_name": "pressure"}},
  {"range": {"@timestamp": {"gte": "<@timestamp>", "lte": "<end>"}}}]}}}
```

Juju errors and warnings during a test:

```json
GET ci-logs-juju-*/_search
{"sort": ["@timestamp", "line"], "query": {"bool": {"filter": [
  {"term": {"bundle_id": "<bundle_id>"}}, {"terms": {"level": ["ERROR", "WARNING"]}},
  {"range": {"@timestamp": {"gte": "<@timestamp>", "lte": "<end>"}}}]}}}
```

Failed hooks, which the status snapshots can miss:

```json
GET ci-logs-juju-*/_search
{"sort": ["@timestamp", "line"], "query": {"bool": {
  "filter": [{"term": {"bundle_id": "<bundle_id>"}}, {"term": {"module": "juju.worker.uniter.operation"}}],
  "must": [{"match_phrase": {"message": "failed"}}]}}}
```

Juju status snapshots during a test where a unit was in error, blocked or waiting:

```json
GET ci-juju-status-*/_search
{"sort": ["@timestamp"], "query": {"bool": {"filter": [
  {"term": {"bundle_id": "<bundle_id>"}},
  {"terms": {"units.workload_status": ["error", "blocked", "waiting"]}},
  {"range": {"@timestamp": {"gte": "<@timestamp>", "lte": "<end>"}}}]}}}
```

One bundle's kernel log, in order:

```json
GET ci-logs-host-*/_search
{"size": 1000, "sort": ["line"], "query": {"bool": {"filter": [
  {"term": {"bundle_id": "<bundle_id>"}}, {"term": {"file": "kernel.log"}}]}}}
```

## Cluster setup

The user needs, on `ci-*`, the index permissions `create_index` and `write`, plus the cluster
permission `cluster_composite_ops` for bulk loading. Each run also installs the templates in
`templates/`, which needs the cluster permissions `cluster:admin/component_template/put` and
`indices:admin/index_template/put`. A file without `index_patterns` is a component template, such
as `ci-logs-common` with the fields every log index shares. Without the permissions, the script
warns and loads with the templates already in place. An admin can install them once, components
first:

```bash
cd .github/actions/ingest/templates
for f in $(grep -L index_patterns *.json) $(grep -l index_patterns *.json); do
  kind=_index_template; grep -q index_patterns "$f" || kind=_component_template
  curl -u admin --cacert ca.pem -X PUT -H 'Content-Type: application/json' \
    "https://opensearch.example.com:9200/$kind/${f%.json}" -d @"$f"
done
```

A template applies when an index is created, so each run also adds the templates' new fields to the
existing indices. `write` allows that. Documents loaded before a new field existed become searchable
on it once their bundle is loaded again. A field that changes type cannot be updated in place: the
script warns, and that index needs a reindex.

## Limits

- Lines of a multi-line log entry are separate documents. Search finds each line, and sorting by
  `line` shows them in order.
- A Kubernetes line split into partial (`P`) records stays split.
- `pytest.log` lines usually have no time. Use the test documents for timing.
- In `job.log`, the `step` of a `run:` step is the first line of its script, such as `set -x`, not
  the step's `name:`. The raw job log only has `##[group]Run <first line>`. A `uses:` step shows
  the action, such as `actions/checkout@<sha>`.
