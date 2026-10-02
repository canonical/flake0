# OpenSearch Index: Classification Records

This defines the OpenSearch index that stores the **LLM's classification
output** for a failing integration test — the "Publish the conclusion to
OpenSearch" step in `docs/architecture_overview.png`, which runs after
evidence (logs, metrics, source diff) has already been ingested into
OpenSearch and after the LLM has produced a verdict using the procedure in
`skills/integration-test-diagnosis/SKILL.md`.

This index is deliberately **lean**: raw logs, metrics, and diffs live in
their own evidence index/indices (ingested earlier in the pipeline, outside
the scope of this doc). A classification document only stores the
structured verdict plus pointers (`evidence_refs`) back to the evidence
documents it was based on — mirroring the diagram's "short/digestible
evidence" + "link to the previously ingested docs".

Field names and allowed enum values here are a direct mirror of
`docs/taxonomy.md`. If the taxonomy changes, update this mapping (and bump
`schema_version`) in the same change.

## Index naming & lifecycle

Classification records accumulate indefinitely (one document per failing
test per CI run), so the index uses a **rollover alias** rather than a
single fixed index:

- Write alias: `flake0-classifications`
- Backing indices: `flake0-classifications-000001`, `-000002`, ...
- Rollover + retention is managed by an Index State Management (ISM)
  policy (`scripts/opensearch/ism-policy.json`): roll over hot indices by
  age/size, then delete after a retention window.

Rationale: this lets dashboards and queries always target the stable alias
name, while the backing index count and retention window can be tuned
operationally without touching ingestion code.

Defaults chosen below (adjust in `ism-policy.json` to taste):
- Rollover at 30 days or 50,000 docs, whichever comes first.
- Delete backing indices after 180 days.

## Document id

Use a **deterministic `_id`** so re-running ingestion (e.g. after a
transient failure) overwrites rather than duplicates:

```
_id = sha256(f"{correlation.repo}:{correlation.run_id}:{correlation.job_id}:{test.name}")
```

## Field reference

### `correlation` — identifies the CI run/job this classification belongs to

| Field | Type | Description |
|---|---|---|
| `correlation.repo` | keyword | e.g. `canonical/mongodb-operator` |
| `correlation.run_id` | keyword | GitHub Actions run id |
| `correlation.job_id` | keyword | GitHub Actions job id |
| `correlation.job_name` | keyword | Job name within the workflow |
| `correlation.workflow` | keyword | Workflow file/name |
| `correlation.pr_number` | integer | Null for non-PR events (e.g. scheduled runs on `main`) |
| `correlation.commit_sha` | keyword | Commit under test |
| `correlation.ref` | keyword | Branch ref |
| `correlation.event_name` | keyword | `pull_request` / `push` / `schedule` / ... |

These fields are also expected on the evidence documents ingested earlier,
so `correlation.run_id` + `correlation.job_id` is the join key across
indices even when `evidence_refs` (below) isn't populated for a given item.

### `test` — the failing test itself

| Field | Type | Description |
|---|---|---|
| `test.name` | keyword | Full pytest nodeid or spread test id |
| `test.charm` | keyword | Juju charm under test, e.g. `mongodb-operator` |
| `test.suite` | keyword | e.g. `integration` |
| `test.status` | keyword | `failed` / `error` |
| `test.duration_seconds` | float | Test duration |

### `classification` — the taxonomy verdict (see `docs/taxonomy.md`)

| Field | Type | Allowed values / notes |
|---|---|---|
| `classification.verdict` | keyword | `REAL_FAILURE` \| `FLAKY` \| `INCONCLUSIVE` |
| `classification.root_cause` | keyword | `code_regression` \| `code_preexisting` \| `test_defect` \| `workload_defect` \| `environment_infra` \| `dependency_drift` \| `external_service` \| `unknown` |
| `classification.confidence_pct` | integer | 0-100, derived from the evidence-completeness formula in `docs/taxonomy.md`, **not** an LLM self-reported number |
| `classification.confidence_bucket` | keyword | `high` (80-100) \| `medium` (50-79) \| `low` (0-49) |
| `classification.recommendation` | keyword | `block_merge` \| `open_issue` \| `fix_test` \| `quarantine_test` \| `retry_safe` \| `escalate_infra` \| `needs_human_review` |
| `classification.rationale` | text | Human-readable narrative explaining the verdict |

These four fields must always be reported independently — never collapsed
into one combined string (per `docs/taxonomy.md` "Reporting format").

### `evidence_signals` — confidence formula inputs (nested)

One entry per signal that was evaluated while computing `confidence_pct`,
so the score is auditable instead of a "vibe".

| Field | Type | Notes |
|---|---|---|
| `evidence_signals.name` | keyword | `diff_correlation` \| `run_history` \| `resource_metrics` \| `workload_logs` \| `package_version_diff` \| `external_incident` \| `environment_setup` |
| `evidence_signals.status` | keyword | `corroborating` \| `missing` \| `contradicting` (matches the +15/-20/-30 terms in `docs/taxonomy.md`) |
| `evidence_signals.detail` | text | One-line note on what was found |

### `evidence_refs` — pointers to previously-ingested evidence docs (nested)

| Field | Type | Notes |
|---|---|---|
| `evidence_refs.kind` | keyword | `pytest_log` \| `juju_debug_log` \| `workload_log` \| `prometheus_metrics` \| `node_exporter` \| `spread_setup_log` \| `source_diff` \| `env_info` |
| `evidence_refs.index` | keyword | Evidence index name the doc lives in |
| `evidence_refs.doc_id` | keyword | `_id` of the evidence document |
| `evidence_refs.description` | text | Optional free-text note |

The evidence index(es) themselves are out of scope for this doc — define
them when the "ingest evidence" step is implemented, but keep `kind` values
and the `correlation.*` fields consistent so these refs (and the fallback
run_id/job_id join) work.

### `github` — trace back to the posted PR conclusion

| Field | Type | Notes |
|---|---|---|
| `github.label` | keyword | Label applied to the PR, e.g. `flaky`, `bug` |
| `github.comment_url` | keyword | URL of the PR comment with the digestible summary |

### `llm` — provenance of the model call that produced this record

| Field | Type | Notes |
|---|---|---|
| `llm.model` | keyword | e.g. `claude-sonnet-4.5` |
| `llm.provider` | keyword | e.g. `anthropic` |
| `llm.skill_version` | keyword | Git ref/version of `skills/integration-test-diagnosis/SKILL.md` used |
| `llm.taxonomy_version` | keyword | Git ref/version of `docs/taxonomy.md` used |
| `llm.raw_output` | text, `index: false` | Full raw LLM response, stored for audit, not searched |
| `llm.prompt_tokens` / `llm.completion_tokens` | integer | Optional cost tracking |

### Top-level housekeeping

| Field | Type | Notes |
|---|---|---|
| `@timestamp` | date | When this classification was published |
| `schema_version` | keyword | Bump when this mapping changes incompatibly |

## Example document

```json
{
  "@timestamp": "2026-10-02T14:03:11Z",
  "schema_version": "1.0",
  "correlation": {
    "repo": "canonical/mongodb-operator",
    "run_id": "11234567890",
    "job_id": "31234567890",
    "job_name": "integration-test (juju 3.6/stable)",
    "workflow": "ci.yaml",
    "pr_number": 512,
    "commit_sha": "a1b2c3d4e5f6",
    "ref": "refs/pull/512/merge",
    "event_name": "pull_request"
  },
  "test": {
    "name": "tests/integration/test_sharding.py::test_cluster_removal",
    "charm": "mongodb-operator",
    "suite": "integration",
    "status": "failed",
    "duration_seconds": 184.2
  },
  "classification": {
    "verdict": "FLAKY",
    "root_cause": "environment_infra",
    "confidence_pct": 65,
    "confidence_bucket": "medium",
    "recommendation": "retry_safe",
    "rationale": "Node exporter shows CPU throttling on the runner during teardown; no related code diff; prior runs on main pass consistently."
  },
  "evidence_signals": [
    { "name": "resource_metrics", "status": "corroborating", "detail": "CPU steal time spiked to 90% during test window" },
    { "name": "diff_correlation", "status": "missing", "detail": "No diff touches sharding removal logic" },
    { "name": "run_history", "status": "corroborating", "detail": "Same test passed on last 5 main runs" }
  ],
  "evidence_refs": [
    { "kind": "node_exporter", "index": "flake0-evidence-metrics-000003", "doc_id": "f3a9...", "description": "Node exporter scrape for run window" },
    { "kind": "pytest_log", "index": "flake0-evidence-logs-000012", "doc_id": "9c21...", "description": "Full pytest output" }
  ],
  "github": {
    "label": "flaky",
    "comment_url": "https://github.com/canonical/mongodb-operator/pull/512#issuecomment-999999"
  },
  "llm": {
    "model": "claude-sonnet-4.5",
    "provider": "anthropic",
    "skill_version": "1e11f67",
    "taxonomy_version": "1e11f67",
    "raw_output": "...",
    "prompt_tokens": 8421,
    "completion_tokens": 612
  },
  "ingested_at": "2026-10-02T14:03:12Z"
}
```

## Bootstrapping

The index template, ISM policy, and initial write index are defined in
`scripts/opensearch/`:

- `index-template.json` — mapping + settings applied to every
  `flake0-classifications-*` backing index.
- `ism-policy.json` — rollover/retention policy.
- `create_index.sh` — idempotent bootstrap script; run it once per
  OpenSearch cluster/environment.

```bash
OPENSEARCH_URL=https://localhost:9200 \
OPENSEARCH_USER=admin \
OPENSEARCH_PASSWORD=... \
./scripts/opensearch/create_index.sh
```

## Open follow-ups

- The evidence index(es) referenced by `evidence_refs` aren't defined yet —
  define them when the "ingest evidence" step (upstream of this one, per
  `docs/architecture_overview.png`) is implemented, keeping `kind` and
  `correlation.*` naming consistent with this doc.
- Auth model for the OpenSearch cluster (basic auth vs IAM vs mTLS) depends
  on where it's hosted; `create_index.sh` currently assumes basic auth.
- Rollover/retention numbers in `ism-policy.json` are starting defaults —
  revisit once real document volume and dashboard/alerting needs are known.
