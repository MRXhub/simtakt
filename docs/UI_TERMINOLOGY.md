# Web UI terminology

Use the same meaning in both languages. Name the user action or the information shown; keep backend object names in technical details.

| 中文 | English | Meaning |
| --- | --- | --- |
| 仿真模板 | Simulation template | Reusable model inputs, parameter settings and result configuration. |
| 实验研究 | Study | A group of runs using a particular template version. |
| 运行仿真 | Run simulation | Configure and submit a simulation run. |
| 算法运行 | Algorithm runs | Algorithm execution records, linked studies, events and results. |
| 计算资源 | Compute resources | Queues, concurrent license allocations and execution nodes. |
| 运行性能 | Run performance | Historical elapsed time, success rates and resource measurements. |
| 运行配置 | Execution configuration | One task class, node, configuration version and CPU allocation. |
| 任务类别 | Task class | Tasks sharing a simulation definition and computation settings; raw candidate parameters are excluded from the grouping key. |
| 执行节点 | Execution node | A configured place to execute simulations; a host can contain multiple nodes. |
| 许可资源池 | License pool | A shared pool of simulation license capacity. |
| 状态核对中 | Checking run state | Confirming whether a previous run remains active or has finished. |
| 样本较少 | Limited samples | Fewer than five recorded runs; reaching five does not guarantee reliable estimates. |
| 已完成 | Completed | Execution has finished. |
| 已通过 | Passed | Result validation has passed. |

## Copy rules

- Write short actions and concrete descriptions. Avoid literal translations such as 形状, 物理墙上耗时, 制品, and 向研究容器注入候选解.
- Retain familiar technical terms when they help: CPU, JSON, TCAD, SPICE, and domain parameter names. Explain RSS and timing definitions in optional help.
- Keep names, units, counts and status meanings consistent across navigation, headings, filters, tooltips and accessibility labels. Translate both entries together in `control_plane/web/static/i18n.js` and preserve matching placeholders.
- Show internal IDs, content hashes, API field names and raw payloads only after a deliberate details action. Display labels must never replace backend identifiers in requests.
- State what the data measures. Sample counts include success and failure; elapsed-time statistics use successful runs with durations. Missing measurements are not zero. Five observations do not establish statistical confidence.
- Use “—” or “Not reported” for unavailable measurements and limits. Do not display invented defaults as actual resource capacity.

Route names and backend contracts remain unchanged, including `/api/shapes` and `#/shapes`.
