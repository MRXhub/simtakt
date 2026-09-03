# Minimal Runtime Example

Run the complete self-contained example from the repository root:

```text
python examples/minimal-runtime/run_demo.py
```

The demo drives a real governed runtime end to end and cleans up after itself.
The expected terminal status is `qualified`.

## What the demo does

`run_demo.py` performs the following steps in order:

1. **Clean state.** It deletes and recreates two disposable areas: `.runtime/`
   (the workspace) and `data/` (the control-plane SQLite database). A stale
   `data/` would leak queued/qualifying evaluations (keyed by a deterministic
   idempotency key) and parameter-schema revisions, so the demo is hermetic only
   from a clean start.
2. **Register the input package.** It writes `.runtime/input-package/` with an
   `input.txt` and a `manifest.json`, computes the revision as the sha256 of the
   manifest, and writes the artifact-registry shard
   `records/artifacts/package.minimal.input.json` (kind `input-package`,
   `active`, workspace location). This is the auditable input baseline.
3. **Register a schema, problem, candidate, and evaluation.** A ParameterSchema
   referencing that `source_package` is registered, then a problem whose
   `simulation_capabilities` is `["minimal-simulation"]`, a candidate over
   parameter `x`, and an `EvaluationRequest` are submitted (→ `queued`).
4. **Assemble the governed runtime.** It calls `compose_runtime` on the example
   root, which reads `project/` and builds the worker, resource monitor,
   dispatcher, and preparation phase the same way a deployed application does.
5. **Run the loop.** A `RuntimeLoop` runs up to five single-round passes; after
   each round it prints the evaluation status. It stops early on `qualified`.
6. **Report.** If the evaluation did not qualify, it prints the stored
   `EvaluationUnresolved` reason. It always prints the terminal status.
7. **Cleanup.** On exit it removes the registered artifact shard and the
   `.runtime/` and `data/` trees. No generated files remain in the working tree.

The fake worker materializes its package during `start_session`, and
`collect_session` returns a `SolverRunRecord` + session result whose evidence
drives the adapter `qualify` report to `qualified`.

## Configuration

Project declarations live under `project/`:

* `RUNTIME_COMPONENTS.json` declares the `worker` and `resource_monitor`
  components (each with `module`, `factory`, `interface_version`) and the
  top-level `scheduling_policy` binding.
* `SIMULATION_ADAPTERS.json` registers the `minimal-simulation` adapter with its
  `capabilities` and `resource_defaults`.
* `EXECUTION_TARGETS.json` declares the local formal execution target.
* The scheduling-policy artifact body is in `config/scheduling-policy.json` and
  its catalog record in `records/artifacts/configuration.project-scheduling-policy.minimal.json`.

`minimal_components.py` provides the fixed-quota `FixedQuotaResourceMonitor`,
the fake `MinimalWorker`, and the `MinimalSimulationAdapter`.

## Replacing the example

Swap the fixed worker and monitor in `minimal_components.py` for site-specific
implementations (a real monitor should query the local CPU/memory, the site
scheduler such as Slurm, and the approved license service). An adapter's
`materialize_package(evaluation_input, task)` is invoked by the worker at
session start and writes a package into the adapter's own configured directory
(`.runtime/packages/`); the returned package is **not** automatically registered
as an artifact — the auditable input base is the `input-package` artifact the
ParameterSchema `source_package` references. Keep `SIMULATION_ADAPTERS.json`,
`EXECUTION_TARGETS.json`, the policy artifact, and its revision in
`RUNTIME_COMPONENTS.json` consistent when you change resource requirements.
