# Flake0
Flake0 is an AI-driven project for classification of failures in the software development lifecycle.
Its goal is to support accelerate the understanding of failure types in the feedback loop of 
Continuous Integration, by automatically examining test failures and categorizing them based on all
available information. This includes test logs, environment setup, infrastructure capacity or code changes,
all of which could lead to "test flakyness" or "real failures".

The design of the project is currently still in draft, and the implementation a work-in-progress.

## Host Telemetry Collection

Flake0 provides a host telemetry collection GitHub Action (`.github/actions/collect`) to record runner health (PSI, CPU, memory, disk, block I/O, network) during CI test runs and upload evidence bundles for post-mortem analysis:

```yaml
- name: Start collection
  uses: canonical/flake0/.github/actions/collect@main
  with:
    phase: start

- name: Run integration tests
  run: pytest tests/integration

- name: Stop collection
  uses: canonical/flake0/.github/actions/collect@main
  if: always()
  with:
    phase: stop
```

See [`.github/actions/collect/README.md`](.github/actions/collect/README.md) for details on inputs, outputs, overhead, and configuration.

Flake0 also provides a `plot` action (`.github/actions/plot`) that renders a runner-health
dashboard PNG straight from a `collect` bundle, with zero configuration:

```yaml
- name: Render dashboard
  if: always()
  uses: canonical/flake0/.github/actions/plot@main
  with:
    bundle-path: ${{ steps.collect.outputs.bundle-path }}
    artifact-name: flake0-plot-${{ github.run_id }}
```

See [`.github/actions/plot/README.md`](.github/actions/plot/README.md) for details.

## Community and support

Flake0 is an open-source project that welcomes community contributions, suggestions, fixes and constructive feedback.

[Contact us on Matrix](https://matrix.to/#/#charmhub-data-platform:ubuntu.com)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the repository structure, and further instructions.
