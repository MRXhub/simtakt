# Docker 部署 / Docker deployment

本文对应仓库根目录的 [Dockerfile](../Dockerfile) 和 [compose.yaml](../compose.yaml)。镜像包含 Python 控制平面、Web 界面、OpenSSH 客户端和参考示例；实际求解器运行在你配置的执行节点上。

提供两个独立入口：`demo` 用于查看界面，`project` 用于连接已有项目。当前提供的是基础容器部署配置，商业求解器、通用 SSH Worker 和服务器自动安装器需要另外接入。

## 1. 准备 Docker

Linux 服务器安装 [Docker Engine](https://docs.docker.com/engine/install/) 和 [Compose 插件](https://docs.docker.com/compose/install/linux/)。Windows 或 macOS 可安装 [Docker Desktop](https://docs.docker.com/desktop/)，并使用 Linux containers。操作系统安装步骤以对应官方文档为准。

确认引擎已启动，当前用户能够运行：

```bash
docker version
docker compose version
```

`docker version` 应同时显示 Client 和 Server。只有客户端、提示无法连接 daemon 时，先启动 Docker 服务。

## 2. 启动演示界面

从仓库根目录执行：

```bash
docker compose --profile demo config --quiet
docker compose --profile demo up -d --build
docker compose --profile demo ps
```

打开 **http://127.0.0.1:8321/**。演示使用内存数据，默认只读，不需要准备项目目录。首次构建需要下载 Python 基础镜像与 OpenSSH 软件包。

查看日志与停止：

```bash
docker compose --profile demo logs --tail 100 demo
docker compose --profile demo down
```

若要在镜像内跑通一次最小评测：

```bash
docker compose run --rm --user 10001:10001 --workdir /opt/simtakt demo python examples/minimal-runtime/run_demo.py
```

预期输出包含 `evaluation terminal status: qualified`。这个命令使用测试适配器，生成的数据随一次性容器清理。

## 3. 准备持久化项目

真实项目必须先配置好 Worker、资源监测组件、仿真适配器、执行节点和调度策略。参见 [adapter 接入规范](ADAPTERS.md)与[最小项目声明](../examples/minimal-runtime/project/)。创建空目录不能代替这些配置。

示例目录结构：

```text
/srv/simtakt/project/
├── project/
│   ├── RUNTIME_COMPONENTS.json
│   ├── SIMULATION_ADAPTERS.json
│   └── EXECUTION_TARGETS.json
├── config/                  # 调度策略等配置正文
├── records/artifacts/       # 版本化文件目录记录
├── extensions/
│   └── site_simtakt/        # 自定义 Python Worker、adapter、monitor
│       └── __init__.py
├── data/                    # 数据库、输入包及输出
└── .runtime/                # 自定义 Worker 的本地持久化记录
```

Compose 将整个项目绑定挂载到 `/workspace`。数据库固定保存在：

```text
/workspace/data/outputs/evaluation-middleware/control.sqlite3
```

Web 和 runtime 必须使用同一个挂载目录；SQLite、模型文件及其版本目录都需要持久化。绑定目录应位于 Docker 主机的本地磁盘；不要把 SQLite 目录放在跨主机共享文件系统上作为扩容方案。

在仓库根目录创建本地 `.env`：

```dotenv
SIMTAKT_PROJECT_ROOT=/srv/simtakt/project
SIMTAKT_HTTP_PORT=8321
SIMTAKT_UID=10001
SIMTAKT_GID=10001
```

`.env` 不进入 Git。Windows Docker Desktop 可使用 `SIMTAKT_PROJECT_ROOT=C:/simtakt/project`。这是 **Docker 主机路径**，不是仿真服务器路径。

镜像默认以 UID/GID `10001:10001` 运行。为新建 Linux 项目目录设置所有权可用：

```bash
sudo install -d -m 0750 -o 10001 -g 10001 /srv/simtakt/project
```

已有目录应按实际管理账户设置 `SIMTAKT_UID` 和 `SIMTAKT_GID`，并确保该账户能读配置、写数据库和输出目录。配置采用 `create_host_path: false`，路径拼错时会报错，而不会悄悄生成一个空项目。

## 4. 启动项目

先停止占用同一端口的演示实例，再检查并启动项目：

```bash
docker compose --profile demo down
docker compose --profile project config --quiet
docker compose --profile project up -d --build
docker compose --profile project ps
docker compose --profile project logs --tail 100 web runtime
```

| 服务 | 作用 | 对外端口 |
| --- | --- | --- |
| `web` | 提供模型导入、模板、研究和运行提交界面。 | 主机 `127.0.0.1:8321`。 |
| `runtime` | 读取队列，准备任务，调度、观察、收集并判定结果。 | 无。 |

Web 的健康检查只证明 HTTP 服务可访问，不证明 Worker、SSH、许可服务或求解器已经就绪。运行时状态应结合日志和一次实际评测确认。只运行 Web 不会处理排队任务。

容器内部服务需要监听 `0.0.0.0`，因此可写 Web 命令显式使用 `--allow-remote-writes`。Compose 仍把主机发布端口限制在回环地址；该开关不会增加身份认证。共享服务器上的其他容器也应属于可信环境。

远程访问时，可以先通过 SSH 隧道使用本地浏览器：

```bash
ssh -N -L 18321:127.0.0.1:8321 control-user@control-host
```

随后打开 **http://127.0.0.1:18321/**。`control-host` 是 Docker 控制平面主机；仿真执行节点可以是另一台机器。面向多人提供访问时，再配置带身份认证的反向代理。

## 5. 安装自己的扩展

镜像的 `PYTHONPATH` 包含 `/opt/simtakt` 和 `/workspace/extensions`。例如：

```text
/workspace/extensions/site_simtakt/factories.py
```

对应配置中的模块名为 `site_simtakt.factories`。工厂收到的是完整的 JSON entry，项目根路径不会自动作为第二个参数传入。容器内路径请使用 `/workspace/...`，不要填 Windows 主机路径。

如果扩展需要第三方 Python 包，可基于本地镜像构建站点镜像：

```dockerfile
FROM simtakt:local
USER root
COPY requirements-site.txt /tmp/requirements-site.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements-site.txt
USER 10001:10001
```

先构建基础镜像，再构建站点镜像，并在自己的 Compose override 中调整 `build` 和 `image`。`requirements-site.txt` 由站点维护；核心不会自动安装第三方求解器依赖。

SSH 使用方式和只读凭据挂载见 [仿真端 SSH 接入](SSH_SIMULATION.md)。私钥和真实连接参数不放入 Dockerfile、镜像或 adapter 目录记录。

## 6. 更新与备份

更新代码后重建并替换控制平面容器：

```bash
git pull --ff-only
docker compose --profile project up -d --build
```

远端长任务能否在控制平面重启后继续被接管，取决于 Worker 对 `resume_session` 的实现。先用小任务验证恢复，再用于长时间计算。此配置默认运行一个 runtime；增加副本前需要验证跨进程资源锁和站点调度语义。

备份时先停止 `web` 和 `runtime`，再备份整个项目目录，包含 SQLite 相关文件、输入包、输出、配置与工件目录记录：

```bash
docker compose --profile project stop web runtime
# 使用站点的备份工具复制完整的 SIMTAKT_PROJECT_ROOT。
docker compose --profile project start web runtime
```

停掉控制平面不等于终止仿真服务器上的任务。若外部程序也会写入项目目录，备份前应协调这些写入。

## 常见问题

| 现象 | 检查方向 |
| --- | --- |
| `no service selected` | 选择 `--profile demo` 或 `--profile project`。 |
| 端口已占用 | 停止旧实例，或修改 `SIMTAKT_HTTP_PORT`。不要同时启动使用相同端口的两个 profile。 |
| bind source 不存在 | 检查 Docker 主机上的项目路径。 |
| `Permission denied` / `readonly database` | 检查 UID/GID、父目录权限及挂载是否可写。 |
| `module import failed` | 检查扩展模块、依赖和容器内 `PYTHONPATH`。 |
| 页面正常但任务不运行 | 检查 runtime 日志、项目声明、目标资源及 adapter 能力匹配。 |
| `Host key verification failed` | 核对 SSH 主机指纹与挂载的 `known_hosts`，不要关闭校验。 |

Compose profile 的行为遵循 [Docker 官方说明](https://docs.docker.com/compose/how-tos/profiles/)；卷、端口和健康检查字段见 [Compose services reference](https://docs.docker.com/reference/compose-file/services/)。
