# Minimal Runtime Example

Run the complete self-contained example from the repository root:

```text
python examples/minimal-runtime/run_demo.py
```

The demo creates a disposable `.runtime/` workspace, writes and registers a
small input package in the artifact catalog, registers a schema that references
that package, and registers a problem whose simulation capability matches the
`minimal-simulation` adapter. It then submits a candidate and evaluation,
runs several bounded runtime rounds, and prints the resulting evaluation
status. The expected terminal status is `qualified`.

## Configuration

Project declarations live below `project/`:

* `RUNTIME_COMPONENTS.json` defines the worker and fixed-quota resource monitor.
* `SIMULATION_ADAPTERS.json` registers the minimal adapter and its capability.
* `EXECUTION_TARGETS.json` declares the local formal execution target.
* The scheduling policy artifact is in `config/` and its catalog record is in
  `records/artifacts/`.

The demo creates any transient control-plane state it needs and removes it on
exit. No generated files should remain in the working tree.

## Replacing the example

Replace the fixed worker and monitor in `minimal_components.py` with
site-specific implementations. An adapter's `materialize_package` receives an
input template and candidate parameters and must write a package into the
provided workspace. Update the adapter catalog, target declaration, and policy
capacity envelope together when changing resource requirements.
