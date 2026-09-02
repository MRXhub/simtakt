# Minimal Runtime Example

This example assembles the governed control-plane runtime and runs three bounded
rounds. Run it from the repository root with `python examples/minimal-runtime/run_demo.py`,
or directly with:

```text
python -m control_plane.runtime --project-root examples/minimal-runtime --max-rounds 3
```

## Configuration

All project declarations live below `project/`:

* `RUNTIME_COMPONENTS.json` has `schema_version: 1` and a `components` array.
  Each component supplies `name`, importable `module`, `factory`, and integer
  `interface_version`. The worker and resource monitor names are required.
  Component `config` values are passed to the factory.
* `PROJECT_STATE.json` must be schema version 2 and active. Its
  `scheduling_policy` reference contains exactly `artifact_id`, `revision`, and
  active `status`.
* `EXECUTION_TARGETS.json` contains a `targets` array. Each target requires a
  token `target_id`, `status`, and boolean `formal_execution`; optional
  `host_id` and `license_pool_id` identify shared capacity.

The policy artifact is in `config/scheduling-policy.json` and its registry
record is in `records/artifacts/`. The policy requires capacity fields
`processors`, `memory_bytes`, `license_sessions`, `baseline_processors`, and
`baseline_memory_bytes`, plus priority and preparation timing fields. Keep the
registry revision equal to the SHA-256 of the policy bytes.

## Replacing the example

Change the component module/factory and component `config` in
`project/RUNTIME_COMPONENTS.json`. Replace the fixed values in
`minimal_components.py` with authoritative local CPU/memory, scheduler, and
license-service adapters (TODO(adapter) marks the integration point), and
update the policy capacity envelope to match. Update target declarations and
regenerate the artifact revision according to your installed version.

The lock uses atomic exclusive file creation and is intentionally only a
portable example; use the site's distributed allocation/locking authority for
multiple hosts.
