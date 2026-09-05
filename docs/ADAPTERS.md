# Adapter 开发与注册 / Adapter integration

本文说明现有 Python 接口与项目 JSON 格式。Docker 加载方式见 [Docker 部署](DOCKER.md)，远端连接约定见 [仿真端 SSH 接入](SSH_SIMULATION.md)。

`site_simtakt` 是本文使用的**自定义模块名称**，仓库没有内置这个模块。以下配置可作为接入模板；要执行真实仿真，需先提供相应工厂及实现。若只想运行现成示例，请使用 [minimal-runtime](../examples/minimal-runtime/README.md)。

## 1. 三个组件分别负责什么

| 组件 | 职责 | 注册位置 |
| --- | --- | --- |
| Simulation adapter | 生成求解器输入包、验证文件、提供 Gateway、提取指标并判定结果。 | `SIMULATION_ADAPTERS.json`。 |
| Worker | 启动、恢复、观察、收集和终止执行会话。 | `RUNTIME_COMPONENTS.json` 的 `worker`。 |
| Resource monitor | 查询节点资源与许可，提供调度锁和决策记录。 | `RUNTIME_COMPONENTS.json` 的 `resource_monitor`。 |

适配器由准备阶段按能力匹配加载；Worker 和资源监测组件由运行时启动时组装。注册 adapter 不会自动替换 Worker，也不会自动获得 SSH 执行能力。

一个运行时配置一个 Worker 对象。要管理多个求解器或执行节点，该 Worker 需要按计划中的 `simulation_proxy`、`target_id` 等绑定进行路由。

## 2. 模块与工厂

容器中的扩展目录可以是：

```text
/workspace/extensions/site_simtakt/
├── __init__.py
├── factories.py
├── adapter.py
├── worker.py
└── monitor.py
```

`factories.py` 的入口形式：

```python
from .adapter import SolverAdapter
from .worker import SolverWorker
from .monitor import SiteResourceMonitor


def create_adapter(entry):
    return SolverAdapter(entry)


def create_worker(entry):
    return SolverWorker(entry)


def create_monitor(entry):
    return SiteResourceMonitor(entry)
```

这段代码约定三个类接收完整 entry；这些类需要你实现。工厂签名为 `factory(entry)`，不会额外收到 `project_root` 参数。`config` 是传给扩展的自定义数据，由扩展自己验证，不能假定核心会解释其中字段。

`module` 必须能被 `importlib.import_module` 导入，`factory` 必须是该模块上的可调用对象。密钥等敏感信息使用外部文件引用，不把内容写进目录记录。加载工厂会执行 Python 代码，只加载可信的站点扩展。

## 3. 仿真适配器目录

文件：`project/SIMULATION_ADAPTERS.json`。

```json
{
  "schema_version": 1,
  "catalog_id": "simulation-adapters",
  "adapters": [
    {
      "adapter_id": "example-solver",
      "status": "experimental",
      "module": "site_simtakt.factories",
      "factory": "create_adapter",
      "interface_version": 1,
      "capabilities": [
        "example-simulation"
      ],
      "performance_class_id": "performance-class:sha256:51f1d2c7e3f44d97774d96f02657b80b7a6e8646a1db564810dee9cb7023937b",
      "resource_defaults": {
        "processors": 1,
        "memory_bytes": 1073741824,
        "max_wall_seconds": 10800
      },
      "config": {
        "package_dir": "/workspace/.runtime/packages"
      }
    }
  ]
}
```

| 字段 | 约定 |
| --- | --- |
| `schema_version` / `catalog_id` | 当前为 `1` / `simulation-adapters`。 |
| `adapter_id` | 项目内唯一且稳定的名称；返回对象的 `adapter_id` 必须一致。 |
| `status` | `active`、`experimental` 或 `disabled`；前两者均可参与匹配。 |
| `module` / `factory` | Python 模块与工厂函数名称。 |
| `interface_version` | adapter 当前支持整数 `1`。 |
| `capabilities` | 非空、不重复的能力字符串数组。 |
| `resource_defaults` | 提供处理器数量、内存字节数和声明的运行预算。 |
| `performance_class_id` | 性能分类标识，格式为 `performance-class:sha256:` 加 64 位小写十六进制。当前准备链路需要有效值。 |
| `config` | 扩展自己的配置；核心将其随 entry 传入工厂。 |

上例声明请求 1 个处理器、1 GiB 内存和 3 小时预算。**3 小时是此示例的配置值，不是全局默认或所有任务的绝对上限。** 超长仿真应按实际情况设置预算，并与站点队列时限匹配。`kill_multiplier` 等恢复策略位于调度策略中，不是 SSH 超时选项，详见[运行预算](ARCHITECTURE.md#learned-wall-budgets-and-kill-thresholds)。

目录读取器会检查资源数值是有限、非负的数；实际运行计划还要求正整数处理器数和秒数。接入配置应提供正整数 `processors`、`memory_bytes`、`max_wall_seconds`，避免目录加载通过后在准备阶段失败。

示例性能分类标识由 `example-solver:1:default` 的 SHA-256 生成，仅标识计算类别，不代表已经做过性能测量。自己的分类应采用稳定的计算配置标识，计算含义变化时更新：

```python
import hashlib

identity = "my-solver:version-1:default-mode"
print("performance-class:sha256:" + hashlib.sha256(identity.encode()).hexdigest())
```

不声明 `simulation_definition` 时，核心从 adapter entry 内容推导版本身份。修改能力、资源或配置会改变这个身份；已持久化计划仍保留自己的绑定。不要在运行中随意覆盖旧版本所依赖的实现或文件。

### 能力如何匹配

Problem 的 `simulation_capabilities` 必须是且仅是一个可运行 adapter 的能力子集。例如 `['example-simulation']` 能匹配上例。零个匹配或多个匹配都会失败；配置顺序不会用来消除歧义。

`experimental` 不等于关闭。如果暂时不希望某个 adapter 被选中，将其设为 `disabled`。Web 模板中的工具选项同样来自该目录，但目录可见不代表其 Python 模块已加载或求解器已经可用。

## 4. Worker、资源组件与执行节点

`project/RUNTIME_COMPONENTS.json` 的结构：

```json
{
  "schema_version": 1,
  "scheduling_policy": {
    "artifact_id": "configuration.project-scheduling-policy.minimal",
    "revision": "sha256:57865955aa490df9b8cc1ce4cc8f3e4666ea4587dafc705130a60ba47467336d"
  },
  "components": [
    {
      "name": "worker",
      "module": "site_simtakt.factories",
      "factory": "create_worker",
      "interface_version": 1,
      "config": {
        "session_directory": "/workspace/.runtime/sessions"
      }
    },
    {
      "name": "resource_monitor",
      "module": "site_simtakt.factories",
      "factory": "create_monitor",
      "interface_version": 1,
      "config": {
        "target_id": "solver-linux-01"
      }
    }
  ]
}
```

其中的调度策略引用取自可运行的最小示例。要复用该策略，需要同时复制其 [配置正文](../examples/minimal-runtime/config/scheduling-policy.json)和[目录记录](../examples/minimal-runtime/records/artifacts/configuration.project-scheduling-policy.minimal.json)。该示例策略上限为 4 个处理器、1 GiB 内存和 2 个许可会话；实际项目应登记符合站点容量的策略，并同步这里的 artifact ID 和 revision。改变配置正文后不能继续沿用旧 revision。

`project/EXECUTION_TARGETS.json`：

```json
{
  "targets": [
    {
      "target_id": "solver-linux-01",
      "host_id": "solver-host-01",
      "license_pool_id": "solver-license",
      "status": "active",
      "formal_execution": true,
      "allowed_operations": [
        "simulation"
      ],
      "processors": 8
    }
  ]
}
```

`target_id` 是逻辑执行节点，`host_id` 表示共享物理主机，`license_pool_id` 表示共享许可池。它们都不会自动解析成 SSH 地址。只允许 `active` 且 `formal_execution: true` 的节点参与正式执行。

Worker 的 SSH 映射、资源监测器的 `target_id`、计划中的目标与目录必须一致。目标的 `processors` 可参与当前准备阶段的容量筛选，但静态目录不能代替监测器报告的实时容量。多个正式节点需要明确 `host_id`，监测器还需提供跨目标的 `locked_dispatch`。

## 5. Adapter 接口

接口定义：[SimulationAdapter](../control_plane/simulation/adapter_protocol.py)。

```python
class SimulationAdapter:
    adapter_id: str

    def build_gateway(self, context): ...
    def materialize_package(self, evaluation_input, task): ...
    def validate_package(self, context, task, preparation, package): ...
    def qualify(self, middleware, attempt_id, context): ...
```

这是接口摘要，不是能直接运行的默认实现。

| 方法 | 返回与行为 |
| --- | --- |
| `build_gateway(context)` | 返回满足 [SimulationGateway](../control_plane/simulation/gateway.py) 的对象，将求解器启动、回执、重试和文件发布等操作封装在扩展中。 |
| `materialize_package(evaluation_input, task)` | 返回字符串映射，通常包含 `artifact_id`、`revision`、`path`；由 Worker 在启动会话时调用。 |
| `validate_package(...)` | 成功返回 `None`，无法使用时抛出明确异常。 |
| `qualify(middleware, attempt_id, context)` | 返回合法的 QualificationReport，结合指标、证据和数值收敛条件判定。 |

准备阶段会核对模板 `source_package` 引用的输入基线。启动时生成的运行包不会自动注册到工件目录；扩展需处理其持久化、版本与证据发布，不能仅返回一个不可解析的临时文件路径。

Gateway 接口包括启动调用、`observe`、`recover_launch`、`collect_runner_receipt`、`publish_run_artifact`、`retry_action`、`start_retry`、`recover_retry` 和 `register_artifact`。它与 Worker 的方法名不同，不应把一个普通 SSH 连接对象直接当作 Gateway。

## 6. Worker 接口与状态

接口定义：[SimulationWorker](../control_plane/simulation/worker.py)。

```python
class SimulationWorker:
    def start_session(self, plan, allocation, session_ref) -> None: ...
    def resume_session(self, plan, allocation, session_ref) -> None: ...
    def observe_session(self, session_ref) -> str: ...
    def collect_session(self, session_ref) -> tuple: ...
    # 可选：没有终止能力时不必实现。
    def terminate_session(self, session_ref) -> str: ...
```

`plan` 是已绑定的 [SimulationSessionPlan](../control_plane/simulation/session_contracts.py)，包含尝试与评测身份、输入包、目标、资源和预算。`allocation` 由调度器持久化。Worker 不直接操作队列或更改 SQLite 中的评测状态。

| 接口 | 接受的状态或返回值 |
| --- | --- |
| `observe_session` | `running`、`completed`、`absent`、`unreachable`、`indeterminate`。 |
| `terminate_session` | `terminated`、`absent`、`unreachable`、`indeterminate`。 |
| `collect_session` | 二元组 `(session_result, result_artifact_id)`。 |

`completed` 表示执行结束、可收集，不保证数值结果成功。`absent` 需要确认该会话不存在；网络中断或证据不足不能返回 `absent`。`resume_session` 只恢复绑定，不重新启动已有任务。重复启动与收集要围绕稳定的 `session_ref` 和远端作业身份实现幂等。

启动失败可抛出 `SessionStartFailure(outcome, failure_class, message)`；`outcome` 可为 `not_started`、`preflight_failed`、`absent`、`unreachable` 或 `indeterminate`。未知是否已经启动时，保留可恢复信息，不自动再提交一个作业。

## 7. 结果与测量格式

使用 [session_contracts.py](../control_plane/simulation/session_contracts.py) 的构造器生成结果，不手写或猜测内容哈希：

- `make_solver_run_record(...)`：单次求解记录，状态为 `completed`、`failed` 或 `indeterminate`。
- `make_simulation_session_result(...)`：会话结果，状态为 `completed`、`exhausted` 或 `indeterminate`。
- `make_qualification_report(...)`：位于 [evaluation_contracts.py](../control_plane/core/evaluation_contracts.py)，状态为 `qualified`、`ambiguous` 或 `rejected`。

`qualify` 的只读 context 至少包含 `evaluation_id`、`candidate_id`、`attempt_ids` 和 `artifact_ids`。通过这些身份和 middleware 找到输入与证据，再计算报告；不要依赖数据库内部表布局。

成功报告需要实际指标与证据；进程退出码为零并不等于数值收敛。无效输入、确定的计算失败和网络状态不明也应分别表达。

| 测量字段 | 单位与含义 |
| --- | --- |
| `wall_seconds` | 求解操作本身的耗时，秒。 |
| `cpu_seconds` | 求解使用的累计 CPU 时间，秒，可大于 wall time。 |
| `peak_rss_bytes` | 可获得时提供峰值常驻内存，字节。 |

无法获得的测量用 `None`，不要伪造为零或用整个会话寿命替代。除了 `solver_run_record_ids`，可在 `make_simulation_session_result` 的 `solver_run_records` 中附上合法记录正文，并持久化对应证据。仅传记录 ID 不会自动从远端读取测量值。

当前完成接口在 `feedback=None` 时不会自动生成性能反馈，默认分发器也没有额外传入 feedback。接入方若需要历史性能画像，应通过服务层的 `record_attempt_feedback(...)` 提交经过核实的终态测量，或在自己的完成调用中显式提供 feedback；不要让 Worker 直接写数据库。结果可收集与性能画像已更新需要分别验证。

## 8. Resource monitor 接口

至少实现 `locked_snapshot(target_id)` 和 `record_decision(...)`；多目标运行时还需 `locked_dispatch()`。可以参考 [FixedQuotaResourceMonitor](../examples/minimal-runtime/minimal_components.py) 的形状，但它的固定额度仅用于测试，不能代替真实服务器与许可监测。

快照字段包括目标、可用 CPU/内存、许可占用、已观察到的分配身份、远端工作根目录、时间和锁状态。方法签名见 [ResourceMonitor](../control_plane/core/ports.py)，快照用法见[准备任务分发器](../control_plane/evaluation/prepared_dispatcher.py)。锁必须覆盖实际会共享容量的运行时进程；仅给每个对象放一个独立内存锁无法协调多个调度进程。

## 9. 本地检查顺序

1. 用 `load_catalog(project_root)` 检查目录格式；它不导入求解器代码。
2. 用 `resolve_adapter(project_root, adapter_id)` 验证工厂与 adapter 接口；这一步会执行扩展代码。
3. 使用 `compose_runtime(project_root)` 验证 Worker、监测器、拓扑与调度策略，并在完成后 `close()`。
4. 提交小任务，验证启动、结果、重复调用、重启恢复、失联与终止路径。
5. 检查实际指标、证据及许可释放，再进行长任务验证。

可运行的教学示例：

```bash
python examples/minimal-runtime/run_demo.py
python examples/adapter-local-process/run_demo.py
python examples/adapter-batch-queue/run_demo.py
python examples/adapter-server-session/run_demo.py
```

后面三个示例分别说明本地进程、批处理队列和持久服务会话的行为；它们不是填入一组商业求解器地址即可上线的通用插件。
