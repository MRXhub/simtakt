# Architecture

## Layered responsibilities

The control plane is organized into layers with explicit boundaries:

- **Core** defines evaluation contracts and dependency-inversion interfaces. It
  contains the data shapes and validation rules shared by every deployment.
- **Evaluation** turns requests into governed preparation plans, ranks work,
  dispatches selected plans, and records lifecycle state. Preparation, the
  scheduling policy, resource observations, and durable receipts live in this
  layer.
- **Data** provides durable evaluation and study repositories. Repository
  implementations may use SQLite or another store while preserving the core
  contracts.
- **Simulation** owns the runtime-neutral worker, gateway, adapter, and session
  protocols. It is the only layer that needs to know how a particular simulation
  runtime is started, observed, recovered, and collected.
- **Web** presents read-only status views. It consumes projections from the
  evaluation services and does not perform scheduling or runtime control.

## Runtime assembly from project files

A governed runtime is built by `control_plane.runtime.composition.compose_runtime`
reading three declarations under `project/` — it is **not** wired by
hand-constructing and injecting a set of ports into a service:

- **`project/RUNTIME_COMPONENTS.json`** is the mandatory assembly document
  (`composition.py`). Its `components` must name both a **worker** and a
  **resource_monitor** entry, each with `module`, `factory`, and integer
  `interface_version`; the factory object must expose the required session /
  resource methods. The document's top-level `scheduling_policy`
  (`artifact_id` + `revision`) is the current binding for the active scheduling
  policy artifact (`scheduling_policy.resolve_governed_scheduling_policy`).
- **`project/SIMULATION_ADAPTERS.json`** registers concrete simulator adapters
  (see below). Preparation selects one adapter per problem by capability.
- **`project/EXECUTION_TARGETS.json`** declares the execution targets. Only
  targets that are `active` and `formal_execution: true` participate in
  scheduling (`execution_topology.parse_execution_topology`).

Composition fails closed when any of these guarantees is violated (missing or
malformed components file, required worker/monitor absent, required methods
missing, or — see Deployment guards — a multi-target monitor that cannot provide
a global dispatch lock).

## Preparation does not read PROJECT_STATE

Preparation is governed from the project declarations above, not from a
`PROJECT_STATE.json` envelope. `preparation_phase.PreparationPhase` resolves the
source package from the registered ParameterSchema's `source_package`
(`preparation_phase._make`), and the policy binding comes from
`RUNTIME_COMPONENTS.json.scheduling_policy`. Governance
(`governed_preparation.validate_policy_derived_execution_preparation`) seals the
same choices. The `ControlStore` port and its `PROJECT_STATE.json` reader remain
only for legacy project-level calibration-admission paths; ordinary runtime
preparation never reads it. Full field-by-field detail is in
[`docs/governed-preparation-inputs.md`](governed-preparation-inputs.md).

## Dependency-inversion ports

`control_plane/core/ports.py` defines replaceable storage/observation
boundaries that decouple the control plane from a host project's file layout:

1. **ControlStore** reads governed project control state.
2. **ArtifactStore** resolves a registered artifact record by identity.
3. **TargetCatalog** reads the execution-target catalog.
4. **ResourceMonitor** provides a locked, read-only resource snapshot for a
   scheduling decision, records decision receipts, and — for multi-target
   dispatch — a cross-target `locked_dispatch` lock.

File-backed reference implementations are provided for these ports and can be
swapped for your own storage. Replacing a port means providing an object that
satisfies the corresponding Python `Protocol` and constructing it in the
application composition root. (Note: runtime *components* such as the worker and
resource monitor are themselves concrete instances built from
`RUNTIME_COMPONENTS.json`, and simulator adapters are catalog registrations,
not storage ports.)

## Adding an adapter

Adapters connect a concrete simulator (commercial TCAD, multiphysics, batch
solvers, ...) to the runtime-neutral simulation layer:

1. Implement the **worker** protocol (`control_plane/simulation/worker.py`): it
   starts or resumes a session, observes its state, and collects a durable
   result without accessing the evaluation queue. Optional `terminate_session`
   enables confirmed termination.
2. Implement the **gateway** protocol used by the worker — launching the
   runtime, confirming launch, observing and recovering, validating runner
   receipts, publishing run artifacts, and exposing retry operations.
3. Implement the adapter (`control_plane/simulation/adapter_protocol.py`):
   `build_gateway`, `materialize_package`, `validate_package`, and `qualify`.
   Keep tool commands, filesystem details, and credentials inside this adapter.
4. Register the adapter in `project/SIMULATION_ADAPTERS.json` with an
   `adapter_id`, `status` (`active`/`experimental`/`disabled`), `module`,
   `factory`, integer `interface_version`, the non-empty `capabilities` the
   adapter covers, and `resource_defaults` with `processors`, `memory_bytes`,
   and `max_wall_seconds`. `simulation_definition` is optional: when absent the
   preparation binds a content-derived identity
   `configuration.adapter.<adapter_id>` over the canonical catalog-entry JSON
   (`simulation/adapter_catalog.simulation_definition_identity`).
5. Preparation resolves the single runnable adapter whose capabilities are a
   superset of the problem's `simulation_capabilities`. Disabled entries, no
   match, or an ambiguous multiple match all fail closed rather than being
   selected implicitly (`resolve_adapter_for_problem`).

Adapters are not "started up and injected into a dispatcher." Worker and
resource_monitor come from `RUNTIME_COMPONENTS.json`; the adapter is resolved
lazily during preparation when a queued evaluation needs it.

### Package materialization

`adapter.materialize_package(evaluation_input, task)` is an adapter behavior
executed by the worker **when it starts a session** — it is not part of the
preparation phase, and the package it writes is **not** automatically registered
as an artifact. The auditable input baseline is the input-package artifact
referenced by the ParameterSchema `source_package` at an exact revision, which
preparation validated and bound into the immutable preparation.

## Failure and duration semantics

- Session failure classes `unreachable` and `indeterminate` are distinct from
  `absent` and are never collapsed into it (`worker.SESSION_*`). The dispatcher
  observes a reconciling session before applying wall-proof recovery
  (`service.auto_release_wall_budget` / `dispatcher.recover_once`); a pending
  session is only confirmed terminated when the worker returns `terminated` or
  `absent`, and a monitor/worker without `terminate_session` reports
  termination as unconfirmed.
- Measured solve durations flow from worker collection: each `SolverRunRecord`
  carries `wall_seconds` / `cpu_seconds` / `peak_rss_bytes`; completion feedback
  writes them to `attempt_feedback`, where multiple solver records are summed
  for wall/cpu and RSS takes the max, feeding the per-task compute profile.
  Missing measurements stay `None`.
- Preparation itself is a lazy queued phase with bounded claims; a bad input
  releases the claim and marks that evaluation `unresolved` without aborting the
  round.

## Deployment guards

A runtime refuses to start (rather than degrade) on several conditions
(`composition.compose_runtime`, `execution_topology.ensure_formal_targets_ready`,
`prepared_dispatcher`):

- `project/RUNTIME_COMPONENTS.json` is missing, not valid JSON, lacks
  `schema_version == 1`, or does not declare a `worker` and a `resource_monitor`
  with the required methods.
- More than one formal execution target is declared without each carrying a
  `host_id` (missing host identity is never inferred to be a private host), or
  the resource monitor does not provide `locked_dispatch` for the multi-target
  case.
- A disabled adapter is never selected; a problem whose capabilities match no
  adapter or more than one adapter is not implicitly scheduled; a non-active /
  non-formal target is not scheduled; and policy/revision or capacity-out-of-
  range choices fail closed.

## Demo architecture

The demo (`examples/minimal-runtime/`) uses the same composition boundary as a
deployed application. It builds a disposable workspace and SQLite database,
registers a real input-package artifact and a ParameterSchema referencing it,
registers a problem, and then calls `compose_runtime` so that the genuine
runtime assembles the worker and resource monitor from
`project/RUNTIME_COMPONENTS.json` and drives a real preparation →
dispatch → session → collection → qualification loop. A fixed-quota resource
monitor and a fake worker/adapter emulate runtime launch and receipt handling
without an external simulator, host, or license service. The read-only web
status server can consume the same repository projections. A production
composition replaces the example worker/monitor through the same project-file
mechanism and adapter registration.

## The lifecycle of one evaluation

`requested` → `queued` → (lazy preparation) → scheduled & dispatched → session
running → `qualifying` → `qualified` / `unresolved`. Recovery or wall-proof
failure can requeue an evaluation to `queued`, and one bad input is isolated as
`unresolved`. See the [project README](../README.md) section *The lifecycle of
one evaluation* and the [governed preparation inputs](governed-preparation-inputs.md)
document for the detailed inputs and rules.
