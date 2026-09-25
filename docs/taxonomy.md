# Failure Classification Taxonomy

This is the canonical taxonomy used by Flake0 to classify a failing Juju charm
integration test. Any agent, skill, or downstream consumer (GitHub PR label,
OpenSearch document) that classifies or reads a classification must use these
exact values.

Classification uses **two independent axes**. They must be reported and
reasoned about separately, never collapsed into a single field: a failure can
be non-deterministic (`FLAKY`) *and* still be caused by a real bug in the
code under test (e.g. a race condition introduced by the PR). Treating that
combination as "just flaky" and waving it off is the most common and most
costly classification error, so the two axes exist specifically to prevent it.

## Axis 1 — Verdict (determinism)

Whether the failure is reproducible given the same code and environment.

| Value | Definition | Evidence needed |
|---|---|---|
| `REAL_FAILURE` | Reproducible given the same code + environment. | Fails consistently on retry/same commit, or the root cause is structural (bad assertion, missing wait, genuine bug). |
| `FLAKY` | Same code, same environment, inconsistent outcome across runs. | History shows pass/fail alternation on unchanged commits; no structural cause found. |
| `INCONCLUSIVE` | Not enough signal to decide. | No history, missing logs, single occurrence, ambiguous or contradictory evidence. |

## Axis 2 — Root cause category

Why the failure happened, scoped to the evidence Flake0 actually collects
(source diff, spread setup logs, node exporter metrics, pytest/juju
debug-log, workload logs/sosreport, prometheus scrape — see `AGENTS.md`).

| Category | Definition | Primary evidence source |
|---|---|---|
| `code_regression` | Bug in the charm code introduced by this PR/commit. | Source diff; failure maps to a changed code path. |
| `code_preexisting` | Bug in charm code, unrelated to this PR. | Same failure reproduces on `main`/unrelated PRs; no relevant diff. |
| `test_defect` | Bad assertion, missing wait/retry, bad fixture/teardown, timing assumption in the test itself. | pytest traceback points at test code, not charm/workload code. |
| `workload_defect` | The managed application (e.g. MongoDB, OpenSearch) misbehaves, not the charm logic. | Workload logs/sosreport show app-level crash/error; juju debug-log shows the charm handled it correctly. |
| `environment_infra` | Runner resource exhaustion, network blip, kernel/image issue. | Node exporter metrics show CPU/mem/disk pressure at failure time; spread setup log errors. |
| `dependency_drift` | snap/juju/charm-lib version change breaks behavior, unrelated to the PR's own diff. | Package version diff between last-good and failing run; no correlation with the PR's code diff. |
| `external_service` | GitHub Actions, Charmhub, snap store, or cloud provider outage. | Failure timing correlates with a known external incident; error is a connection/5xx to an external endpoint. |
| `unknown` | Insufficient evidence to pick a category. | — |

## Confidence

Confidence is a percentage (0-100), reported alongside a bucket label. Both
are required: the percentage lets the OpenSearch record and dashboards
sort/filter/trend on a continuous value; the bucket label is what gates the
recommendation logic and human-review routing, so a single stray "73%
instead of 74%" can't flip a decision.

The percentage must be **derived from evidence completeness**, not a raw
LLM self-reported number — an unanchored "I'm 85% confident" from the model
is false precision and not comparable across runs. Compute it from how many
of the expected signals for the chosen root cause category (see the
"Primary evidence source" column above, plus run history) are actually
present and consistent:

- Start at 50%.
- +15% for each independent corroborating signal that agrees with the
  chosen root cause (e.g. diff correlation, run history, resource metrics,
  workload logs) — cap the total contribution from corroborating signals.
- -20% for each signal that is missing entirely (not checked/unavailable).
- -30% for any signal that is checked and contradicts the chosen root cause.
- Clamp to [0, 100].

| Bucket | Percentage range | Meaning |
|---|---|---|
| `high` | 80-100 | History, diff, and logs all agree. |
| `medium` | 50-79 | One supporting signal is missing (e.g. no history available, single occurrence). |
| `low` | 0-49 | Evidence is circumstantial or contradictory — this should push the verdict toward `INCONCLUSIVE` regardless of how certain the reasoning sounds. |

Report both, e.g. `confidence: 65 (medium)`. If the bucket derived from the
percentage doesn't match the evidence narrative, trust the percentage
formula and adjust the narrative, not the reverse — the formula exists so
confidence isn't just a vibe.

## Combining the axes → recommendation

The (verdict, root cause) pair maps to a recommended next step. This is what
drives the PR label and the "recommendation for next step" published to
OpenSearch.

| Verdict × Root cause | Meaning | Recommendation |
|---|---|---|
| `REAL_FAILURE` × `code_regression` | New bug introduced by this PR. | `block_merge` |
| `REAL_FAILURE` × `code_preexisting` | Known/existing bug, not this PR's fault. | `open_issue` (don't block this PR) |
| `REAL_FAILURE` × `test_defect` | The test itself is broken. | `fix_test` (don't block on product code) |
| `FLAKY` × `code_regression` | Race condition introduced by this PR — still a real bug, just intermittent. | `block_merge` (do **not** auto-quarantine) |
| `FLAKY` × `code_preexisting` | Pre-existing race condition/bug that surfaces intermittently. | `open_issue`, consider `quarantine_test` |
| `FLAKY` × `workload_defect` | The underlying managed application is flaky. | `open_issue` against workload; consider `quarantine_test` |
| `FLAKY` × `environment_infra` | Classic infra flake. | `retry_safe`, no code action |
| `FLAKY` × `dependency_drift` | A new dependency version is unstable. | `escalate_infra` / pin version |
| `*` × `external_service` | Outside the project's control. | `retry_safe`; note the incident; exclude from flaky-rate metrics |
| `INCONCLUSIVE` × `unknown` | Not enough data. | `needs_human_review` |

Any (verdict, root cause) pair not listed above should default to
`needs_human_review` rather than guessing a recommendation.

## Recommendation values

- `block_merge` — do not merge; a real code defect exists.
- `open_issue` — file/link a tracking issue; does not need to block this PR.
- `fix_test` — the test needs fixing, not the product code.
- `quarantine_test` — mark as known-flaky so it doesn't block merges going forward.
- `retry_safe` — safe to re-run; no code or test change implied.
- `escalate_infra` — notify infra/ops (runner, dependency, environment problem).
- `needs_human_review` — evidence is insufficient for an automated decision.

## Reporting format

Any classification (agent report, structured LLM output, OpenSearch
document) must report `verdict`, `root_cause`, `confidence`, and
`recommendation` as independent fields — never as one combined string.
`confidence` itself is two sub-fields: `confidence_pct` (0-100, int) and
`confidence_bucket` (`high`/`medium`/`low`, derived from `confidence_pct`
per the ranges above).
