# 仿真端 SSH 接入 / SSH simulation endpoints

SSH 负责连接仿真服务器；Worker 和 adapter 负责启动求解器、恢复会话、判断状态与收集结果。当前仓库包含这些 Python 接口和参考实现，**尚未内置可直接填入地址使用的通用 SSH Worker**。

本文给出标准 OpenSSH 连接格式、容器挂载方式，以及自定义 Worker 的配置约定。OpenSSH 配置可以直接被 `ssh` 读取；文中的 `config.ssh` 和目标映射属于扩展约定，需要由你的工厂函数实现读取。

## 1. 先明确两台主机的角色

```text
浏览器 → 控制平面主机（Web + runtime + SQLite）
                         │
                         └── SSH → 仿真执行节点（求解器 + 工作目录 + 许可）
```

控制平面通过 SSH 客户端访问执行节点，不需要在控制平面容器中启动 SSH 服务。执行节点需已经安装求解器，具备可用的计算资源和许可，并提供站点批准的任务启动方式。

下面以 Linux/POSIX 执行节点为例。Windows OpenSSH 的远端 shell、路径和进程终止方式不同，应实现对应 Worker，不能照搬 POSIX 作业管理命令。

## 2. 连接格式

交互式诊断常用：

```bash
ssh -p 22 simulation@192.0.2.10
```

`192.0.2.10` 是文档示例地址，需替换。为自动化定义稳定的主机别名，例如 `simtakt-solver`，将端口、账户和密钥位置放入 OpenSSH config。

容器使用的配置文件 `deploy/ssh/config` 示例：

```sshconfig
Host simtakt-solver
    HostName 192.0.2.10
    Port 22
    User simulation
    IdentityFile /run/simtakt-ssh/id_ed25519
    UserKnownHostsFile /run/simtakt-ssh/known_hosts
    IdentitiesOnly yes
    BatchMode yes
    StrictHostKeyChecking yes
    ConnectTimeout 10
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

| 字段 | 含义 |
| --- | --- |
| `Host` | 本地使用的连接别名，不是平台的 `target_id`。 |
| `HostName` / `Port` / `User` | 执行节点地址、SSH 端口和运行账户。 |
| `IdentityFile` | 私钥文件路径。容器内路径与主机路径不同。 |
| `UserKnownHostsFile` | 保存经过核对的服务器主机公钥。 |
| `BatchMode yes` | 禁止交互式密码提示，使后台任务能明确失败。 |
| `StrictHostKeyChecking yes` | 未知或变化的主机密钥需要先核对。 |
| `ConnectTimeout` | SSH 建连超时，不是仿真的最长运行时间。 |
| `ServerAliveInterval` / `ServerAliveCountMax` | 检测无响应连接；不据此判定远端仿真已经停止。 |

这些字段由 [OpenSSH `ssh_config`](https://man.openbsd.org/ssh_config) 定义。在宿主机直接使用时，可将密钥与 known-hosts 路径改为自己的 `~/.ssh/...`，再使用 `ssh -F 配置文件 simtakt-solver`。

需要跳板机时，可在配置中定义第二个 `Host`，并为执行节点设置 `ProxyJump`。私钥保留在控制平面侧，不需要复制到跳板机或求解器目录。

## 3. 准备密钥与主机指纹

推荐使用为该运行账户单独配置的密钥。公钥由服务器管理员登记到执行节点账户的 `authorized_keys`；私钥保留在控制平面主机上。

服务器主机公钥应通过管理员或可信的管理通道核对指纹，再写入 `known_hosts`。`ssh-keyscan` 只能采集对端提供的公钥，不能独立证明其身份；不要通过 `StrictHostKeyChecking no` 跳过这一步。

Linux 上私钥应仅允许实际容器运行账户读取，例如文件权限 `0600`，目录权限 `0700`。文件所有者需与 Compose 的 `SIMTAKT_UID` 对应。Windows 上使用相应的文件 ACL。

本地目录示例：

```text
deploy/ssh/
├── config
├── id_ed25519
└── known_hosts
```

此目录以及 `compose.ssh.yaml` 已被 `.gitignore` 排除。镜像构建采用允许列表，不复制站点项目目录和 SSH 凭据。

## 4. 挂载到 runtime 容器

在本地 `.env` 中增加 Docker 主机上的绝对路径：

```dotenv
SIMTAKT_SSH_DIR=/srv/simtakt/ssh
```

将上述三个文件放入该目录。在仓库根目录创建本地 `compose.ssh.yaml`：

```yaml
services:
  runtime:
    volumes:
      - type: bind
        source: ${SIMTAKT_SSH_DIR:?Set SIMTAKT_SSH_DIR to your SSH directory}
        target: /run/simtakt-ssh
        read_only: true
        bind:
          create_host_path: false
```

这个 override 给 runtime 增加挂载，保留原有 `/workspace` 挂载。Web 容器不需要接触私钥。

先检查配置和 OpenSSH 解析结果：

```bash
docker compose -f compose.yaml -f compose.ssh.yaml --profile project config --quiet
docker compose -f compose.yaml -f compose.ssh.yaml run --rm runtime ssh -G -F /run/simtakt-ssh/config simtakt-solver
```

`ssh -G` 只展开配置，不建立连接。确认配置后，通过无交互短命令验证执行节点可达：

```bash
docker compose -f compose.yaml -f compose.ssh.yaml run --rm runtime ssh -F /run/simtakt-ssh/config -T simtakt-solver true
```

`true` 成功只说明 SSH 与远端 shell 可用，不说明求解器、工作目录权限和许可已经可用。安装好自己的 Worker 与 adapter 后，再启动：

```bash
docker compose -f compose.yaml -f compose.ssh.yaml --profile project up -d --build
```

以后更新、查看日志和停止服务时，同样带上这两个 `-f` 参数。基础镜像内已有 OpenSSH 客户端；仅增加挂载不会自动产生 SSH 调度能力。

## 5. Worker 连接配置约定

下面是建议用于 `RUNTIME_COMPONENTS.json` 的 **worker entry 片段**，不是完整运行时声明：

```json
{
  "name": "worker",
  "module": "site_simtakt.factories",
  "factory": "create_worker",
  "interface_version": 1,
  "config": {
    "session_directory": "/workspace/.runtime/sessions",
    "ssh": {
      "config_file": "/run/simtakt-ssh/config",
      "probe_timeout_seconds": 30
    },
    "targets": {
      "solver-linux-01": {
        "host_alias": "simtakt-solver",
        "remote_workspace_root": "/srv/simtakt/jobs"
      }
    }
  }
}
```

`site_simtakt.factories` 是你需要实现的扩展模块。核心将完整 entry 传给 `create_worker(entry)`，不会解释 `config.ssh`、连接服务器或验证这些自定义字段。工厂应验证配置后再创建 Worker。

| 名称 | 示例 | 负责哪种身份 |
| --- | --- | --- |
| `target_id` | `solver-linux-01` | 平台中的逻辑执行节点；必须与目标目录一致。 |
| `host_id` | `solver-host-01` | 资源核算中的物理主机身份；不是自动解析的 IP。 |
| `host_alias` | `simtakt-solver` | Worker 调用 OpenSSH 时使用的 `Host` 别名。 |
| `remote_workspace_root` | `/srv/simtakt/jobs` | 执行节点上的目录；不是控制平面的 `/workspace`。 |

多执行节点需要显式映射。Worker 应按持久化 plan/allocation 中的 `target_id` 选择连接，不能默认把所有任务发往第一个 SSH 主机。资源监测器也必须报告相同节点与远端工作根目录。

## 6. 长时间仿真的会话约定

SSH 连接的寿命不能代替仿真任务的寿命。站点 Worker 应把启动、观察和收集做成有界的远端操作，由调度系统或可恢复的远端执行服务持有长任务。

| 方法 | SSH 接入时需要做到 |
| --- | --- |
| `start_session` | 用稳定的 `session_ref` 幂等地创建或找回作业；持久化远端 job ID、目标、计划身份和工作目录。 |
| `resume_session` | 根据已保存的绑定重新定位已有作业，不再次启动求解器。 |
| `observe_session` | 查询作业或会话本身；网络故障返回 `unreachable`，证据不足返回 `indeterminate`。 |
| `collect_session` | 核对输入、作业身份和结果文件，发布结果记录；重复调用保持结果身份稳定。 |
| `terminate_session` | 请求取消后，再确认对应作业及需要管理的子进程已经停止。 |

不要仅凭一个 PID 判断作业身份，也不要把 SSH 命令退出码当作数值收敛结果。SSH 客户端返回错误或超时，不足以证明远端任务不存在；作业已结束但计算失败，应通过结果契约表达失败。

控制请求的超时应短且有界，例如上述 `probe_timeout_seconds=30`。仿真的预算使用 adapter 的 `resource_defaults.max_wall_seconds` 及运行计划中的预算字段。长达数十小时的仿真可以设置相应预算；不要因为 SSH 建连超时为 10 秒，就把仿真预算设为 10 秒。当前自动准备流程会把声明预算同时写入 plan 的 `command_timeout_seconds` 和 `max_wall_seconds`；Worker 的短连接/探测超时由扩展单独控制。

## 7. 接入前需要确认的条件

先用一个小任务验证：正常启动与结果收集、重复启动不重复提交、控制平面重启后恢复、SSH 断开后重新观察、请求取消后确认退出。再确认求解器的收敛判据、许可释放方式与结果文件格式。

实现接口与返回值详见 [adapter 开发与注册](ADAPTERS.md)。批处理场景可参考 [batch queue 示例](../examples/adapter-batch-queue/README.md)，持久服务会话可参考 [server session 示例](../examples/adapter-server-session/README.md)。
