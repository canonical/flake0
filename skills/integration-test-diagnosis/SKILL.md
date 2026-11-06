---
name: integration-test-diagnosis
description: Diagnosis of a failing integration test. Use this skill to investigate and classify the root cause of a failing integration test.
---

Identify the root cause of a failing integration test and classify it using
the taxonomy defined in `docs/taxonomy.md`. That file is the source of
truth for all label values — treat it as authoritative and keep this
procedure in sync with it.

Classification has two **independent** axes, reported separately:
- **Verdict** (determinism): `REAL_FAILURE` / `FLAKY` / `INCONCLUSIVE`.
- **Root cause**: `code_regression` / `code_preexisting` / `test_defect` /
  `workload_defect` / `environment_infra` / `dependency_drift` /
  `external_service` / `unknown`.

Do not collapse these into one label. A failure can be `FLAKY` and still be
caused by `code_regression` (e.g. a race condition introduced by the PR) —
that combination must still route to `block_merge`, not be dismissed as
"just flaky." See the combination table in `docs/taxonomy.md`.

## Procedure
1. **Gather Information**: Collect all relevant information about the failing integration test. All what you need will be available locally, this includes code source change, test logs, environment configuration and metrics about resources.

2. **Analyze Test Logs**: Review the test logs and error messages to identify any patterns or specific errors that can indicate the nature of the failure.

3. **Investigate Test Environment**: Check the test environment for any issues that may be affecting the test execution. This includes verifying:
   - Environment variables and configurations
   - Dependency versions and compatibility
   - Resource availability (CPU, memory, disk space)
   - Network connectivity and access to required services

4. **Check Recent Changes**: Examine recent code changes or deployments that may have introduced the failure. Look for:
   - Code commits that affect the functionality being tested
   - Changes in dependencies or configurations
   - Any recent merges or pull requests

5. **Determine the verdict**: decide `REAL_FAILURE`, `FLAKY`, or
   `INCONCLUSIVE` based on run history and whether the failure reproduces
   deterministically. See `docs/taxonomy.md` Axis 1 for the exact
   definitions and required evidence.

6. **Determine the root cause category**: independently of the verdict,
   assign one of `code_regression`, `code_preexisting`, `test_defect`,
   `workload_defect`, `environment_infra`, `dependency_drift`,
   `external_service`, or `unknown`. See `docs/taxonomy.md` Axis 2 for
   definitions and the primary evidence source expected for each.

7. **Assign confidence**: compute a percentage (0-100) from evidence
   completeness using the formula in `docs/taxonomy.md` (start at 50%,
   adjust per corroborating/missing/contradicting signal), then derive the
   bucket (`high`/`medium`/`low`) from that percentage. Do not report a
   free-floating "gut feeling" percentage — it must trace back to which
   signals were present, missing, or contradictory.

8. **Derive the recommendation**: look up the (verdict, root cause) pair in
   the combination table in `docs/taxonomy.md` to get the recommendation
   (`block_merge`, `open_issue`, `fix_test`, `quarantine_test`,
   `retry_safe`, `escalate_infra`, or `needs_human_review`). Any
   uncovered combination defaults to `needs_human_review`.

9. **Document Findings**: Record verdict, root cause, confidence,
   recommendation, and the supporting evidence for each — these must be
   reported as independent fields, not merged into a single string.
