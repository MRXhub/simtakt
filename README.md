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

The five ports in `control_plane/core/ports.py` (`ControlStore`,
`ArtifactStore`, `TargetCatalog`, `ResourceMonitor`, `ProjectMaterializer`)
isolate the control plane from any host project's file layout. File-based
reference implementations are provided; swap them for your own storage.

Concrete simulator adapters (e.g. for commercial TCAD or multiphysics tools) are
intentionally **not** part of this core: implement the worker and gateway protocols and
register via the adapter catalog. See
`docs/ARCHITECTURE.md` for the public architecture and extension points.

## License

MIT — see [LICENSE](LICENSE). If this project is useful in your research, a
citation via [CITATION.cff](CITATION.cff) is appreciated.
