# codex-cli-monitor

`codex-cli-monitor` 是一个用于观察本机 Codex CLI 运行状态的小工具。它的目标是低侵入地显示当前打开了多少个 Codex 会话，并把每个会话归类为少量可直接使用的状态。

## 功能

- 扫描当前系统里的 Codex CLI 进程，显示会话数量、PID、TTY、工作目录、运行时长等信息。
- 根据 Codex hooks、进程树、CPU 变化、网络连接和 Codex 本地状态文件，判断会话状态。
- 区分“确定事实”和“推断状态”，不会把推断结果当成 Codex 内部真实状态。
- 支持 JSON 输出，方便接入脚本或面板。
- 支持常驻后台 HTTP API，供桌面前端轮询当前 Codex 会话状态。
- 支持多服务器采集与聚合：每台服务器本地采集，VPS 聚合服务同时监控自身 Codex 并合并远端状态。
- 提供轻量原生 Win32 小型悬浮窗前端，用状态圆点展示每个 Codex 进程。
- 提供可选的同名 `codex` shim，用来记录启动元数据后再透明执行真正的 Codex CLI。

## 大概原理

默认方式是 sidecar 监控：工具独立运行，不修改 Codex，也不要求改变正常使用习惯。它主要读取这些只读信号：

- `/proc` 里的进程、父子进程、命令行、TTY、当前工作目录和 CPU 时间。
- 进程持有的网络连接，用作远程 API 请求的辅助判断。
- `$CODEX_HOME` 下的本地状态文件元数据，例如 session JSONL 文件路径、大小、修改时间，以及头尾结构化事件类型。
- Codex hooks 写入的 turn/tool/stop 生命周期事件。
- 可选 shim 写入的启动记录。

为了减少误判，远程 API 进行中的判断不会只依赖长连接；它需要近期 Codex session 文件活动和网络连接共同支撑。监控不会输出 session JSONL 的消息正文。

主状态只有三种：

- `运行中`：已提交提示词，AI 正在思考、等待 API、执行 MCP、本地工具或其他操作。
- `成功`：新打开、空闲等待输入，或最近一轮结果完成成功。
- `失败`：从运行中结束，且最近一轮出现 API/模型错误，或被手动 Ctrl+C 中断。

JSON 里仍保留 `inferred_status` 诊断字段，里面可能出现 `waiting_user_likely`、`api_inflight_likely`、`tool_running_likely` 等内部推断值，用来解释证据和置信度；表格和顶层 `status` 只显示上面的三种主状态。

## 使用方法

先安装低侵入 hooks，用来记录 Codex turn/tool/stop 生命周期事件：

```bash
./bin/codex-monitor-install-hooks
```

安装后，在每个正在运行或新打开的 Codex CLI 里执行 `/hooks`，按提示 review/trust 新 hook。这个步骤是 Codex 的安全机制。

信任后，监控会优先使用 hook 事件判断状态：用户提交后到 `Stop` 前视为 `运行中`，`Stop` 后结合 session JSONL 末尾事件判断 `成功` 或 `失败`；新打开或等待输入的 Codex 进程显示为 `成功`。同一工作目录下的多个 Codex 进程会按 Codex PID 和进程启动时间隔离，避免一个进程的会话文件覆盖另一个进程的状态。没有 hook 事件的旧会话会继续使用 sidecar 信号降级推断。

直接在项目目录运行：

```bash
PYTHONPATH=src python3 -m codex_cli_monitor
```

输出 JSON：

```bash
PYTHONPATH=src python3 -m codex_cli_monitor --json
```

单次快照，不采样 CPU：

```bash
PYTHONPATH=src python3 -m codex_cli_monitor --sample-window 0
```

每 2 秒刷新一次：

```bash
PYTHONPATH=src python3 -m codex_cli_monitor --watch 2
```

常驻后台运行 API 服务：

```bash
PYTHONPATH=src python3 -m codex_cli_monitor --daemon
```

默认监听：

```text
http://127.0.0.1:8765
```

停止后台服务：

```bash
PYTHONPATH=src python3 -m codex_cli_monitor --stop
```

前台运行 API 服务，便于调试：

```bash
PYTHONPATH=src python3 -m codex_cli_monitor --serve --host 127.0.0.1 --port 8765
```

查询会话状态：

```bash
curl http://127.0.0.1:8765/api/sessions
```

API 会返回每个 Codex 进程的主状态、目录和启动时间，示例字段如下：

```json
{
  "session_count": 1,
  "sessions": [
    {
      "pid": 1234,
      "status": "运行中",
      "directory": "/work/project",
      "started_at": 1782475200.0,
      "started_at_iso": "2026-06-26T12:00:00Z"
    }
  ]
}
```

指定 Codex 本地状态目录：

```bash
PYTHONPATH=src python3 -m codex_cli_monitor --codex-home ~/.codex
```

## 多服务器部署

多服务器部署包含三个端：

```text
Linux 采集服务器 ─┐
Linux 采集服务器 ─┼─ HTTP(S)/Tailscale ─ VPS 聚合服务 ─ Windows 悬浮窗
VPS 本机 Codex  ──┘                     （同时采集 VPS 本机）
```

- VPS 聚合服务接收远端快照，并使用相同的本地采集逻辑显示 VPS 自己的 Codex。
- 每台 Linux 采集服务器只读取本机 `/proc`、Hook 日志和 Codex session 文件，然后异步推送最小状态快照。
- Hook 始终只写本地文件，不直接访问 VPS；VPS 离线不会阻塞 Codex。
- Windows 悬浮窗只访问聚合服务，不直接连接每台采集服务器。

### 1. 部署前准备

Linux 服务端和采集器是纯 Python，不需要编译，也不需要安装 Python 依赖包。每台 Linux 机器需要：

- Python 3.10 或更高版本
- systemd
- `sudo`，或者直接使用 root 安装 systemd unit
- `git`
- 实际运行 Codex 的普通 Linux 用户
- 推荐启用 NTP、systemd-timesyncd 或 chrony 时间同步

检查环境：

```bash
python3 --version
systemctl --version
git --version
timedatectl status
```

在 VPS 和每台采集服务器 clone 仓库：

```bash
git clone https://github.com/ZeroJehovah/codex-cli-monitor.git
cd codex-cli-monitor
```

仓库跟踪三个模板：

- `start-server.sh.example`：VPS 聚合服务安装脚本
- `start-collector.sh.example`：Linux 采集器安装脚本
- `start-widget.ps1.example`：Windows 登录计划任务安装脚本

实际配置文件 `start-server.sh`、`start-collector.sh` 和 `start-widget.ps1` 已加入 `.gitignore`，可以写入真实 Token，不会被 Git 提交。

### 2. 生成和分配 Token

生成两个不同的随机 Token：

```bash
openssl rand -hex 32
openssl rand -hex 32
```

Token 对应关系：

```text
VPS API_READ_TOKEN
    └── Windows start-widget.ps1 中的 ApiToken

VPS COLLECTOR_WRITE_TOKEN
    └── 每台 Linux start-collector.sh 中的 COLLECTOR_WRITE_TOKEN
```

- 读取 Token 只给 Windows 前端或其他只读 API 客户端。
- 写入 Token 只给采集器。
- 不要把读取 Token 和写入 Token 设置成相同值。
- 示例脚本只允许长度至少 16 的字母、数字、点、下划线、波浪号或短横线 Token；`openssl rand -hex 32` 的输出符合要求。

### 3. 配置 VPS 聚合服务

在 VPS 仓库根目录创建实际配置脚本：

```bash
install -m 700 start-server.sh.example start-server.sh
vim start-server.sh
```

需要修改的配置：

| 配置 | 是否必须修改 | 说明 |
|---|---:|---|
| `SERVICE_USER` | 必须确认 | systemd 服务运行用户，必须是 VPS 上实际运行 Codex 的用户 |
| `SERVER_ID` | 建议修改 | 全局唯一机器 ID，例如 `vps-main`，只能使用字母、数字、`.`、`_`、`:`、`-` |
| `SERVER_NAME` | 建议修改 | Windows 前端显示名称，例如 `Tokyo VPS` |
| `LISTEN_HOST` | 必须确认 | 推荐填写 VPS 的 Tailscale/WireGuard IP；`127.0.0.1` 只能供本机或反向代理访问 |
| `LISTEN_PORT` | 可选 | 默认 `8765` |
| `REMOTE_TTL` | 可选 | 远端采集器多久无更新后移除旧会话，默认 5 秒 |
| `LOCAL_CACHE_SECONDS` | 可选 | VPS 本机扫描缓存，默认 0.25 秒 |
| `INSTALL_HOOKS` | 可选 | `1` 表示自动安装本机 Hook，建议保持 `1` |
| `API_READ_TOKEN` | 必须修改 | Windows 和只读 API 使用的 Token |
| `COLLECTOR_WRITE_TOKEN` | 必须修改 | 所有采集器上报使用的 Token |

`SERVICE_USER` 最重要。假设 Codex 是用户 `alice` 运行的，应配置：

```bash
SERVICE_USER="alice"
```

如果错误地使用 root，而 Codex 实际由普通用户运行，聚合服务会读取 root 的 `~/.codex` 和 Hook 日志，状态准确性会明显下降。

监听示例：

```bash
# Tailscale 地址，推荐
LISTEN_HOST="100.64.0.10"

# 仅供同机 Caddy/Nginx 反向代理访问
LISTEN_HOST="127.0.0.1"

# 所有网卡；必须配合防火墙，通常不推荐直接暴露
LISTEN_HOST="0.0.0.0"
```

可以先生成并验证 systemd unit，不修改系统：

```bash
DRY_RUN=1 ./start-server.sh >/tmp/codex-monitor-aggregator.service
systemd-analyze verify /tmp/codex-monitor-aggregator.service
```

正式安装：

```bash
./start-server.sh
```

脚本会自动执行：

1. 检查 Python 版本、systemd、用户和配置。
2. 为 `SERVICE_USER` 安装 Codex Monitor Hook。
3. 停止旧的自管 `--daemon` 进程。
4. 停止同机的采集器 systemd 服务，避免端口和 PID 文件冲突。
5. 写入 `/etc/codex-cli-monitor/aggregator.env`，权限为 `0600`。
6. 写入 `/etc/systemd/system/codex-monitor-aggregator.service`。
7. 执行 `systemctl enable --now`，设置开机启动并立即运行。

检查服务：

```bash
sudo systemctl status codex-monitor-aggregator.service
sudo systemctl is-enabled codex-monitor-aggregator.service
sudo systemctl is-active codex-monitor-aggregator.service
sudo journalctl -u codex-monitor-aggregator.service -f
```

健康检查不需要 Token：

```bash
curl http://100.64.0.10:8765/healthz
```

查询聚合会话需要读取 Token：

```bash
READ_TOKEN="填写 start-server.sh 中的 API_READ_TOKEN"

curl \
  -H "Authorization: Bearer $READ_TOKEN" \
  http://100.64.0.10:8765/api/sessions
```

查询当前已连接服务器：

```bash
curl \
  -H "Authorization: Bearer $READ_TOKEN" \
  http://100.64.0.10:8765/api/servers
```

如果通过公网访问，应由 Caddy/Nginx 提供 HTTPS、证书、访问日志和限流。不要直接把无 TLS 的 Python HTTP 端口暴露到公网。

### 4. 配置 Linux 采集器

在每台非 VPS Codex 服务器的仓库根目录执行：

```bash
install -m 700 start-collector.sh.example start-collector.sh
vim start-collector.sh
```

需要修改的配置：

| 配置 | 是否必须修改 | 说明 |
|---|---:|---|
| `SERVICE_USER` | 必须确认 | 必须是该服务器实际运行 Codex 的用户 |
| `SERVER_ID` | 必须保证唯一 | 例如 `dev-01`、`gpu-server`，不能与 VPS 或其他采集器重复 |
| `SERVER_NAME` | 建议修改 | Windows 前端显示名称 |
| `AGGREGATOR_URL` | 必须修改 | 聚合服务根地址，例如 `http://100.64.0.10:8765` 或 `https://monitor.example.com` |
| `COLLECTOR_WRITE_TOKEN` | 必须修改 | 必须与 VPS 的 `COLLECTOR_WRITE_TOKEN` 完全相同 |
| `LISTEN_HOST` | 通常不改 | 本机诊断 API，建议保持 `127.0.0.1` |
| `LISTEN_PORT` | 可选 | 本机诊断 API 端口，默认 `8765` |
| `COLLECTOR_INTERVAL` | 可选 | 上报间隔，默认 0.5 秒 |
| `LOCAL_CACHE_SECONDS` | 可选 | 本机扫描缓存，默认 0.25 秒 |
| `INSTALL_HOOKS` | 可选 | `1` 表示自动安装本机 Hook，建议保持 `1` |

Tailscale 直连示例：

```bash
AGGREGATOR_URL="http://100.64.0.10:8765"
```

公网反向代理示例：

```bash
AGGREGATOR_URL="https://monitor.example.com"
```

程序在根地址后自动追加 `/api/collector/snapshot`。也可以直接填写完整上报端点。

先进行 dry-run：

```bash
DRY_RUN=1 ./start-collector.sh >/tmp/codex-monitor-collector.service
systemd-analyze verify /tmp/codex-monitor-collector.service
```

正式安装：

```bash
./start-collector.sh
```

脚本会安装 Hook、将 Token 写入 `/etc/codex-cli-monitor/collector.env`，并创建、启用：

```text
/etc/systemd/system/codex-monitor-collector.service
```

检查采集器：

```bash
sudo systemctl status codex-monitor-collector.service
sudo systemctl is-enabled codex-monitor-collector.service
sudo systemctl is-active codex-monitor-collector.service
sudo journalctl -u codex-monitor-collector.service -f
```

本机诊断 API：

```bash
curl http://127.0.0.1:8765/healthz
curl http://127.0.0.1:8765/api/sessions
```

在 VPS 查询 `/api/servers`，应当能看到该采集器的 `SERVER_ID`。如果采集器断联超过 `REMOTE_TTL`，聚合端会移除它的旧会话。

### 5. 配置和信任 Codex Hook

聚合服务和采集器安装脚本默认都会为 `SERVICE_USER` 执行 Hook 安装。也可以手动安装：

```bash
./bin/codex-monitor-install-hooks
```

指定其他 Codex 用户目录：

```bash
./bin/codex-monitor-install-hooks \
  --codex-home /home/alice/.codex \
  --repo-root "$PWD"
```

Hook 配置文件默认位置：

```text
~/.codex/hooks.json
```

Hook 生命周期日志默认位置：

```text
~/.local/state/codex-cli-monitor/hooks.jsonl
```

安装配置后，在每个已经打开或新打开的 Codex CLI 中执行：

```text
/hooks
```

按 Codex 提示 review/trust 新 Hook。这个信任步骤不能由 systemd 安装脚本绕过。

检查 Hook 是否产生事件：

```bash
tail -f ~/.local/state/codex-cli-monitor/hooks.jsonl
```

如果移动了仓库目录，必须重新运行对应安装脚本或 `codex-monitor-install-hooks`，因为 Hook 命令中记录了仓库的绝对路径。

同一台机器如果有多个 Linux 用户分别运行 Codex，需要为每个用户分别安装 Hook 和采集服务，并使用不同的 systemd unit 名称、PID 文件和本机 API 端口；默认模板针对一个 Codex 用户设计。

### 6. 配置 Windows 悬浮窗

Windows 前端需要先存在以下文件：

```text
dist/CodexMonitorWidget-win-x64/CodexMonitorWidget.exe
```

`dist` 被 Git 忽略，因此全新 clone 不会自带 exe。可以从已经构建的机器复制该目录，或者按照后文“构建 Windows x64 exe”步骤在 Linux/WSL 构建。

创建实际配置脚本：

```powershell
Copy-Item .\start-widget.ps1.example .\start-widget.ps1
notepad .\start-widget.ps1
```

修改：

| 配置 | 说明 |
|---|---|
| `$ApiUrl` | 聚合读取地址，必须包含 `/api/sessions` |
| `$ApiToken` | 必须等于 VPS 的 `API_READ_TOKEN` |
| `$TaskName` | 计划任务名称，默认 `CodexMonitorWidget` |
| `$ExePath` | 前端 exe 路径，默认使用仓库 `dist` 目录 |

示例：

```powershell
$ApiUrl = "https://monitor.example.com/api/sessions"
$ApiToken = "填写 VPS 的 API_READ_TOKEN"
```

注册当前用户登录计划任务并立即启动：

```powershell
powershell -ExecutionPolicy Bypass -File .\start-widget.ps1
```

Windows GUI 不能作为 Windows Service 显示在用户桌面，因此脚本使用当前用户登录计划任务。脚本会限制自身 ACL，因为其中保存了明文读取 Token。

管理计划任务：

```powershell
Get-ScheduledTask -TaskName CodexMonitorWidget
Start-ScheduledTask -TaskName CodexMonitorWidget
Stop-ScheduledTask -TaskName CodexMonitorWidget
Unregister-ScheduledTask -TaskName CodexMonitorWidget
```

前端本身具有单实例保护；重复启动不会出现多个悬浮窗。

### 7. 服务管理

聚合服务：

```bash
sudo systemctl start codex-monitor-aggregator.service
sudo systemctl stop codex-monitor-aggregator.service
sudo systemctl restart codex-monitor-aggregator.service
sudo systemctl enable codex-monitor-aggregator.service
sudo systemctl disable codex-monitor-aggregator.service
```

采集器：

```bash
sudo systemctl start codex-monitor-collector.service
sudo systemctl stop codex-monitor-collector.service
sudo systemctl restart codex-monitor-collector.service
sudo systemctl enable codex-monitor-collector.service
sudo systemctl disable codex-monitor-collector.service
```

### 8. 更新代码

systemd 服务直接使用 clone 仓库中的 `src`。更新代码后必须重启对应服务：

VPS：

```bash
cd /path/to/codex-cli-monitor
git pull --ff-only
sudo systemctl restart codex-monitor-aggregator.service
sudo systemctl status codex-monitor-aggregator.service
```

采集服务器：

```bash
cd /path/to/codex-cli-monitor
git pull --ff-only
sudo systemctl restart codex-monitor-collector.service
sudo systemctl status codex-monitor-collector.service
```

如果模板、服务参数、仓库位置或 Hook 配置发生变化，应重新检查实际脚本并再次运行：

```bash
./start-server.sh
# 或
./start-collector.sh
```

不要直接删除或移动正在被 systemd 和 Hook 使用的仓库目录。

### 9. 卸载

卸载 VPS 聚合服务：

```bash
sudo systemctl disable --now codex-monitor-aggregator.service
sudo rm -f /etc/systemd/system/codex-monitor-aggregator.service
sudo rm -f /etc/codex-cli-monitor/aggregator.env
sudo systemctl daemon-reload
```

卸载采集器：

```bash
sudo systemctl disable --now codex-monitor-collector.service
sudo rm -f /etc/systemd/system/codex-monitor-collector.service
sudo rm -f /etc/codex-cli-monitor/collector.env
sudo systemctl daemon-reload
```

删除 systemd 服务不会自动删除 `~/.codex/hooks.json` 中的 Monitor Hook。需要移除时，应编辑该文件并删除包含 `codex_cli_monitor.hooks` 的 Monitor 项，保留其他项目自己的 Hook。

### 10. 常见问题

#### 聚合 API 返回 401

- Windows 或 curl 使用的 Token 必须等于 VPS 的 `API_READ_TOKEN`。
- 采集器上报使用的是另一个 `COLLECTOR_WRITE_TOKEN`，不能用来读取 API。
- 修改 Token 后重新运行安装脚本，或更新 `/etc/codex-cli-monitor/*.env` 后重启服务。

#### VPS 看不到采集服务器

依次检查：

```bash
sudo systemctl status codex-monitor-collector.service
sudo journalctl -u codex-monitor-collector.service -n 100 --no-pager
curl http://127.0.0.1:8765/healthz
```

然后检查采集器是否能访问 VPS：

```bash
curl http://VPS地址:8765/healthz
```

常见原因包括写入 Token 不一致、URL 错误、VPS 只监听 `127.0.0.1`、防火墙阻止端口、HTTPS 证书无效或服务器 ID 重复。

#### 状态一直不准确或一直显示成功

- 确认 systemd 服务的 `SERVICE_USER` 与运行 Codex 的用户一致。
- 在 Codex 中执行 `/hooks` 并确认 Hook 已受信任。
- 检查 Hook 日志是否更新。
- 检查 `~/.codex` 是否属于正确用户。
- 如果移动过仓库，重新安装 Hook。

#### 服务无法启动

```bash
sudo systemctl status codex-monitor-aggregator.service
sudo journalctl -u codex-monitor-aggregator.service -n 100 --no-pager

# 采集器则替换服务名
sudo systemctl status codex-monitor-collector.service
```

常见原因包括端口已被占用、Python 版本过低、仓库路径被移动、`SERVICE_USER` 不存在、Token 仍是占位符或 Tailscale IP 尚未就绪。

#### 安全建议

- 保持实际脚本权限为 `0700`。
- 保持 `/etc/codex-cli-monitor/*.env` 权限为 `0600`。
- 优先使用 Tailscale/WireGuard 私网。
- 公网访问必须使用 HTTPS、反向代理、防火墙和限流。
- 不要把真实 Token 写入 `.example` 文件或 README。
- 定期轮换 Token；轮换后更新两端配置并重启服务。

聚合结果中的每个 session 包含 `server_id`、`server_name`、`server_boot_id`、跨服务器唯一的 `session_key` 和 `server_observed_at`。不同服务器上相同的 PID 或相同目录不会被当作同一会话。

## Windows 悬浮窗

Windows 前端在 `windows/CodexMonitorWidget`。它是一个轻量原生 Win32 小型矩形
桌面悬浮窗，同一 Windows 登录会话内只允许启动一个实例；如果已经运行，再次启动
exe 会直接退出，不会打开第二个悬浮窗。它会轮询 `/api/sessions`，并按目录分组显示
无表头表格：每行第一列是
目录名，第二列是该目录下一个或多个带柔化边缘的 Codex 进程状态圆点。它不依赖
.NET Runtime 或 Electron。

圆点颜色：

- 带呼吸光晕的蓝色：`运行中`
- 绿色：`成功`
- 红色：`失败`

悬浮窗始终置顶，会按目录名宽度、目录行数和每行圆点数量动态调整大小，不为目录名
预留固定大宽度，可以拖动位置并在下次启动时恢复位置。悬浮窗会保持在屏幕工作区内；
如果动态变宽或变高导致越界，会自动贴到对应边缘。右键菜单中的“贴边收纳”选项勾
选时，悬浮窗贴在左侧或右侧后，鼠标移出 1 秒会动态收纳为只显示状态圆点，鼠标移入
会动态展开并可打断正在进行的收纳动画；取消勾选后不会自动收纳。鼠标移到圆点上会
显示 PID、状态、目录和启动时间。右键点击悬浮窗会打开菜单，可以调整显示大小、打开
关于页面或退出程序。

连接多服务器聚合服务时，悬浮窗按服务器和目录共同分组；存在多个服务器时，目录名前会显示服务器名，悬停详情也会显示服务器。Windows GUI 不能通过 Windows Service 显示在用户桌面，因此使用登录计划任务：

```powershell
Copy-Item .\start-widget.ps1.example .\start-widget.ps1
notepad .\start-widget.ps1
powershell -ExecutionPolicy Bypass -File .\start-widget.ps1
```

脚本会保存聚合 API 地址和读取 Token，并注册当前 Windows 用户登录时启动的 `CodexMonitorWidget` 计划任务。实际配置脚本已被 Git 忽略。

构建 Windows x64 exe：

```bash
rm -rf dist/CodexMonitorWidget-win-x64
mkdir -p dist/CodexMonitorWidget-win-x64
resource_obj="$(mktemp /tmp/codex-monitor-widget-resource.XXXXXX.o)"
trap 'rm -f "$resource_obj"' EXIT
x86_64-w64-mingw32-windres -I windows/CodexMonitorWidget/src \
  windows/CodexMonitorWidget/src/resources.rc \
  -O coff -o "$resource_obj"
x86_64-w64-mingw32-gcc -Os -s -DUNICODE -D_UNICODE \
  windows/CodexMonitorWidget/src/main.c \
  "$resource_obj" \
  -o dist/CodexMonitorWidget-win-x64/CodexMonitorWidget.exe \
  -mwindows -municode -Wl,--subsystem,windows \
  -lwinhttp -lcomctl32 -lshell32 -luser32 -lgdi32 -ladvapi32
```

运行生成的 `CodexMonitorWidget.exe` 即可。默认连接
`http://localhost:8765/api/sessions`。如果 API 地址不同，可以传入第一个参数：

```powershell
.\CodexMonitorWidget.exe http://127.0.0.1:8765
```

也可以设置环境变量：

```powershell
$env:CODEX_MONITOR_API_URL = "http://127.0.0.1:8765"
.\CodexMonitorWidget.exe
```

## 可选 shim

如果希望记录 Codex 启动元数据，可以把项目里的 `bin/codex` 放到 `PATH` 前面：

```bash
export PATH="$PWD/bin:$PATH"
codex
```

这个 shim 会把启动记录写到：

```text
${XDG_STATE_HOME:-~/.local/state}/codex-cli-monitor/launches.jsonl
```

然后执行 `PATH` 后面真正的 `codex`。真实 Codex CLI 会收到原始参数，并保持原来的工作目录语义。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```
