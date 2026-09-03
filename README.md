# simtakt

**simtakt** — keeps your simulations on the beat.

A simulator-neutral orchestration control plane for long-running simulation
and evaluation workloads.

It solves a common problem in computational research: you have expensive,
failure-prone jobs (TCAD, FEM, CFD, batch solvers, ...) and you need durable,
auditable orchestration around them — without coupling your scheduling logic to
any particular simulator.

## Highlights

- **Immutable contracts.** Clients submit `Candidate` / `EvaluationRequest`
  descriptions; every Attempt, SessionPlan, and Allocation is persisted as an
  immutable record.
- **Pure scheduler.** The scheduling decision is a pure function over prepared
  option sets, frozen performance profiles, active allocations, and a single
  resource snapshot — deterministic, testable, replayable.
- **Atomic dispatch.** The dispatcher atomically claims the selected Attempt
  and persists the decision before any worker touches a runtime.
- **Pluggable simulators.** Workers and gateways are `Protocol`-typed
  interfaces (`control_plane/simulation`). The core never imports a concrete
  simulator; adapters register through the adapter catalog.
- **Durable state.** A transactional SQLite repository owns
  attempt/session/allocation lifecycle and immutable result projections.
- **Status console.** A read-only web console (`control_plane/web`) with a
  self-contained `--demo` mode — no database or project files required.

## Quickstart

```bash
git clone https://github.com/MRXhub/simtakt.git
cd simtakt

# run the test suite
python -m unittest discover tests

# run the end-to-end local demo (schedules and executes local jobs)
python examples/basic-local/run_demo.py

# launch the status console in demo mode
python -m control_plane.web.status_server --demo
```

The demo in `examples/basic-local/` wires the real scheduler, dispatcher, and
SQLite repository to a trivial local adapter that "simulates" by running short
local jobs — see [examples/basic-local/README.md](examples/basic-local/README.md).

## Layout

| Path | Responsibility |
|------|----------------|
| `control_plane/core` | evaluation contracts, dependency-inversion ports, artifact resolution |
| `control_plane/evaluation` | preparation, scheduling policy, pure scheduler, dispatch, service facade |
| `control_plane/data` | transactional SQLite evaluation repository |
| `control_plane/simulation` | simulator-neutral worker/gateway/adapter protocols |
| `control_plane/web` | read-only status server + console UI (demo mode included) |
| `examples/basic-local` | end-to-end local demo, no external dependencies |
| `tests/` | core test suite |

## Design notes

The dependency-inversion ports in `control_plane/core/ports.py`
(`ControlStore`, `ArtifactStore`, `TargetCatalog`, `ResourceMonitor`) isolate
the control plane from a host project's file layout; file-backed reference
implementations are provided and can be swapped for your own storage.

A governed runtime is **assembled from the project declarations under
`project/`**, not by hand-injecting every port into a service or dispatcher.
`project/RUNTIME_COMPONENTS.json` is the mandatory assembly document: it names
the runtime **worker** and **resource_monitor** components (each with
`module` / `factory` / `interface_version`) and carries the top-level
`scheduling_policy` binding. `project/SIMULATION_ADAPTERS.json` registers
concrete simulator adapters; preparation resolves one adapter per problem by
matching the problem's `simulation_capabilities`. See
`docs/governed-preparation-inputs.md` for the preparation inputs and failure
rules, and `docs/ARCHITECTURE.md` for the runtime assembly and extension points.

Concrete simulator adapters (for commercial TCAD, multiphysics, batch solvers,
...) are intentionally **not** part of this core — implement the worker and the
adapter's gateway/`materialize_package`, declare them under `project/`, and the
runtime picks them up.

## The lifecycle of one evaluation

Submitting an `EvaluationRequest` persists an immutable `Candidate` /
`EvaluationRequest` and moves the evaluation `requested` → `queued`. A **lazy
preparation phase** (`control_plane/evaluation/preparation_phase.py`) claims
queued evaluations within a bounded window and, for each, resolves the adapter
covering the problem's capabilities, reads the ParameterSchema
`source_package` artifact and the adapter `resource_defaults`, and builds one
immutable preparation — reading **no `PROJECT_STATE.json`** and taking no
caller resource input. A bad input (missing package, no or ambiguous adapter,
unknown schema or target) releases the claim and marks that single evaluation
`unresolved`; it never aborts the round.

The pure scheduler and dispatcher then select a prepared option, atomically
claim the Attempt, and drive the worker session. The worker materializes its
package at `start_session`; on completion the collected `SolverRunRecord`
`wall_seconds` become `attempt_feedback` feeding the compute profile. A
successful attempt lets the evaluation enter `qualifying`, where the adapter's
`qualify` report drives the terminal `qualified` / `unresolved`. Session
failure classes (`unreachable` / `indeterminate`) are **not** treated as
`absent`: the dispatcher observes before applying wall-proof recovery, and a
pending session is only confirmed terminated when the worker returns
`terminated` or `absent`. See
[examples/minimal-runtime/README.md](examples/minimal-runtime/README.md) for a
runnable end-to-end example.

When an attempt's session is still running but the attempt itself is released
as lost, the control plane keeps an **orphan session** bound to the live
`session_ref`, so the still-executing job is neither forgotten nor re-run: an
open orphan whose session is observed still `running` defers re-dispatch, open
orphans count against license capacity, and a bounded recovery loop re-observes
them (closing them on absence or TTL expiry, or terminating a session once it
is safe).  If a still-running orphan session later completes, its collected
result is **late-harvested** back into the evaluation through an idempotent,
transactional first-wins check: the first result settles the evaluation while
any duplicate is recorded as `discarded_duplicate` rather than re-opening
qualification.  Each prepared option's wall budget can also be **learned** from
completed samples (with source degradation and automatic widening on the kill
rate) before falling back to the declared budget.  See the *Wall budgets,
orphan recovery, and late harvest* section of
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## License

MIT — see [LICENSE](LICENSE). If this project is useful in your research, a
citation via [CITATION.cff](CITATION.cff) is appreciated.
