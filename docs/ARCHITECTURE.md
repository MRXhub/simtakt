# Architecture

## Layered responsibilities

The control plane is organized into layers with explicit boundaries:

- **Core** defines evaluation contracts and dependency-inversion interfaces. It
  contains the data shapes and validation rules shared by every deployment.
- **Evaluation** turns requests into governed preparation plans, ranks work,
  dispatches selected plans, and records lifecycle state. Scheduling policy,
  resource observations, and durable receipts remain in this layer.
- **Data** provides durable evaluation and study repositories. Repository
  implementations may use SQLite or another store while preserving the core
  contracts.
- **Simulation** owns the runtime-neutral worker, gateway, adapter, and session
  protocols. It is the only layer that needs to know how a particular simulation
  runtime is started, observed, recovered, and collected.
- **Web** presents read-only status views. It consumes projections from the
  evaluation services and does not perform scheduling or runtime control.

Each layer depends on interfaces rather than a host project's directory layout;
applications compose concrete implementations at their boundary.

## The five ports

The `control_plane.core.ports` module defines five replaceable ports:

1. **ControlStore** reads governed project control state.
2. **ArtifactStore** resolves registered artifacts by stable identity.
3. **TargetCatalog** reads the available execution-target catalog.
4. **ResourceMonitor** supplies a locked, read-only resource snapshot for a
   scheduling decision.
5. **ProjectMaterializer** materializes a validated task into an execution input.

To replace an implementation, provide an object satisfying the corresponding
Python protocol, construct it in the application composition root, and inject it
into the evaluation service or dispatcher. The rest of the control plane should
use only the port; no changes to scheduling or domain contracts are required.
A file-backed implementation can therefore be replaced by a database,
service, or managed resource provider independently for each port.

## Adding an adapter

Adapters connect a concrete simulator or commercial TCAD or multiphysics tool to
the runtime-neutral simulation layer:

1. Implement the **worker** protocol. It starts or resumes a session, observes
   its state, and collects a durable result without accessing the evaluation
   queue directly.
2. Implement the **gateway** protocol used by the worker. The gateway launches
   the runtime, confirms launch, supports observation and recovery, validates
   runner receipts, publishes run artifacts, and exposes retry operations.
3. Implement the adapter protocol's package materialization, package
   validation, gateway construction, and qualification operations. Keep tool
   commands, filesystem details, and credentials inside this adapter.
4. Register the adapter in `project/SIMULATION_ADAPTERS.json` with a unique
   identifier, supported interface version, import module, factory, and an
   `active` or `experimental` status. The adapter catalog validates the entry,
   imports the factory, constructs the adapter, and checks its required methods.
5. Resolve the registered adapter at application startup and inject its gateway
   and worker into the dispatcher. Disabled or incompatible registrations fail
   closed rather than being selected implicitly.

This keeps scheduling, persistence, and status views independent of simulator
vendor APIs while allowing adapters to be replaced or upgraded independently.

## Demo architecture

The demo uses the same composition boundaries as a deployed application with
small in-process components: file-backed control and artifact stores, a target
catalog, a deterministic resource monitor, and a project materializer feed the
evaluation service. A lightweight simulation worker and gateway emulate session
launch, observation, recovery, and receipt publication. The read-only web status
server reads the resulting projections and serves the static status page.

Demo mode is intentionally deterministic and local. It exercises the control
flow and protocol wiring without requiring an external simulator, remote host,
license service, or mutable queue access from the web layer. A production
composition can replace each demo component through the same ports and adapter
registration mechanism.
