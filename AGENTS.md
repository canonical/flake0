Flake0 is a project to detect and classify flaky integration tests in the GitHub CI runs of Juju charms. It is scoped specifically to Juju Charms — not general-purpose CI flakiness detection.

## Architecture
Flake0 detects flakiness by using as inputs the following items categorized by:
1. Code Source
    - Repository source code.
    - Changes if the run is after a PR or commit.
2. Environment
    - The runner's info e.g image, size.
    - The packages versions e.g snap, juju.
    - Spread environment setup logs.
    - Node exporter metrics.
3. Logs
    - Integration tests logs e.g pytest, juju debug-log.
    - Workload logs.
    - Prometheus exporter metrics.

The output would be for each integration test saying if this is a flaky integration test or not.
Classification uses the taxonomy defined in `docs/taxonomy.md` (source of truth for verdict, root cause, confidence, and recommendation values) — see `skills/integration-test-diagnosis/SKILL.md` for the procedure.

## Project Structure
The project is structured as follows:
- `docs` - Project documentation and diagrams.
- `scripts` - Scripts used for infrastructure provisioning or for log collection.
- `skills` - A set of skills used by the AI agent.
- `snaps` - Snaps that are used in the log collection or infrastructure provisioning.
- `tests` - Unit / Integration tests of the project.
