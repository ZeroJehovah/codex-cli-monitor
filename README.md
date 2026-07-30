# codex-cli-monitor

`codex-cli-monitor` 是一个用于观察本机 Codex CLI 运行状态的小工具。它的目标是低侵入地显示当前打开了多少个 Codex 会话，并把每个会话归类为少量可直接使用的状态。

## 功能

- 扫描当前系统里的 Codex CLI 进程，显示会话数量、PID、TTY、工作目录、运行时长等信息。
- 使用 Codex 低频 hooks 驱动三态流转，并用最小化结构化终端事件补足失败结果。
- 保留轻量进程存活检查，用于清理未触发 Hook 就意外退出的会话。
- 支持 JSON 输出，方便接入脚本或面板。
- 支持常驻后台 HTTP API，供桌面前端轮询当前 Codex 会话状态。
- 支持多服务器采集与聚合：每台服务器本地采集，VPS 聚合服务同时监控自身 Codex 并合并远端状态。
- 提供轻量原生 Win32 小型悬浮窗前端，用状态圆点展示每个 Codex 进程。
- 提供可选的同名 `codex` shim，用来记录启动元数据后再透明执行真正的 Codex CLI。

## 大概原理

默认方式是 Hook 状态机加轻量 sidecar：工具独立运行，不修改 Codex，也不要求改变正常使用习惯。它只读取这些必要信号：

- `/proc` 里的进程、父子关系、命令行、TTY、当前工作目录、进程启动时间，以及由准确
  Codex PID 持有的 session JSONL 文件描述符。
- Codex hooks 默认只写入 `UserPromptSubmit` 和 `Stop` 两个低频生命周期事件。
- `$CODEX_HOME/sessions` 中与 Hook `session_id` 精确对应、或由已显示 Codex PID 直接持有
  的 session JSONL；读取器增量读取有上限的文件尾。对于 PID 精确绑定的 Goal 自动续跑
  文件，还会读取有上限的生命周期前缀，保证文件变大后开头的 `task_started` 仍可恢复。
  两个区域都只识别结构化 `task_started`、`task_complete.error`、
  `turn_complete.error`、`TurnAborted` 等生命周期记录。
- 可选 shim 写入的启动记录。

常驻扫描不做 CPU 差值采样，不读取进程网络连接，不扫描运行时诊断数据库，也不根据
assistant 文本、工具输出或错误关键词推断状态。监控不会输出 session JSONL 的消息正文。

监控还会读取 Linux 进程的会话 ID、进程组和终端前台进程组。若 Codex 仍残留在
`/proc` 中，但其 TTY 已删除、终端前台进程组已失效，或者原终端会话 leader 已经不存
在，则该进程被视为已确认脱离的终端孤儿，不计入打开的 Codex 会话。仅仅长时间没有输
入或网络流量不是断线证据，正常闲置等待输入的 SSH Codex 仍会显示。

主状态只有三种：

- `运行中`：已提交提示词，AI 正在思考、等待 API、执行 MCP、本地工具或其他操作；若
  同一 Codex PID 已经通过 Hook 显示，Goal 自动续跑创建的新 session 即使没有再次触发
  `UserPromptSubmit`，也可由准确 PID 打开的文件中的结构化 `task_started` 进入运行中。
- `成功`：已显示会话的最近一轮通过 `Stop` 或结构化终端事件完成成功。
- `失败`：从运行中结束，且最近一轮出现 API/模型错误，或被手动 Ctrl+C 中断。

新打开但尚未提交提示词的 Codex 进程不会显示；`SessionStart` 本身也不创建状态行。进程
至少有过一次提交 Hook 后，后续无提交 Hook 的 Goal 自动续跑才可更新该进程已有的状态
行。JSON 为兼容现有调用方仍保留 `inferred_status` 字段，但其中只使用
`running_hook`、`running_terminal`、`success_hook`、`success_terminal` 和
`failure_terminal` 这类确定来源说明，不再包含 CPU、网络或工具活动推断。表格和顶层
`status` 只显示上面的三种主状态。

## 使用方法

先安装低侵入 hooks。默认只记录 `UserPromptSubmit` 和 `Stop`：

```bash
./bin/codex-monitor-install-hooks
```

安装后，在每个正在运行或新打开的 Codex CLI 里执行 `/hooks`，按提示 review/trust 新 hook。这个步骤是 Codex 的安全机制。

信任后，`UserPromptSubmit` 把进程置为 `运行中`，`Stop` 把同一 `turn_id` 置为
`成功`。Codex 的错误和 Ctrl+C 分支不会触发 `Stop`，因此监控还会按 `session_id` 定位
唯一 session 文件，仅增量尾读同一 `turn_id` 的结构化终端事件：非空
`task_complete.error`/`turn_complete.error` 或 `TurnAborted` 置为 `失败`，无错误的完成事件
置为 `成功`。Hook 状态按 PID 和 `session_id` 分开保存；同一 PID 下任何准确绑定的活动
session 都优先于旧的已完成 session。对于已经提交过 Hook 的进程，监控还会检查该 PID
实际打开的 session JSONL，并只用结构化 `task_started` 补足 Goal 自动续跑。自动续跑
文件使用有界生命周期前缀加有界增量尾部，因此长时间运行、文件超过尾读窗口或出现较大
增量缺口后仍能恢复最初的运行标记。已知 ID 冲突绝不会由时间接近度覆盖，也不会按同目录
文件的新旧程度猜测绑定。完全没有提交 Hook 的新进程仍不显示；进程消失时，即使没有
终止 Hook，也会清理其状态行。

直接在项目目录运行：

```bash
PYTHONPATH=src python3 -m codex_cli_monitor
```

输出 JSON：

```bash
PYTHONPATH=src python3 -m codex_cli_monitor --json
```

`--sample-window` 仅作为旧脚本兼容参数保留；当前扫描始终是单次进程快照：

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
- 每台 Linux 采集服务器只读取本机 `/proc`、Hook 日志和 Hook 精确绑定的 Codex session 文件尾，然后异步推送最小状态快照。
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

仓库跟踪两个 Linux 部署脚本模板和一个 Windows 配置模板：

- `start-server.sh.example`：VPS 聚合服务安装脚本
- `start-collector.sh.example`：Linux 采集器安装脚本
- `windows/CodexMonitorWidget/CodexMonitorWidget.ini.example`：Windows 悬浮窗配置模板

实际配置文件 `start-server.sh`、`start-collector.sh` 和
`windows/CodexMonitorWidget/CodexMonitorWidget.ini` 已加入 `.gitignore`，可以写入真实
Token，不会被 Git 提交。Windows 构建目录中的配置文件也位于被忽略的 `dist` 中。

### 2. 生成和分配 Token

生成两个不同的随机 Token：

```bash
openssl rand -hex 32
openssl rand -hex 32
```

Token 对应关系：

```text
VPS API_READ_TOKEN
    └── Windows CodexMonitorWidget.ini 中的 ApiToken

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
| `REMOTE_TTL` | 可选 | 远端采集器多久无更新后移除旧会话，默认 30 秒，可容忍短暂网络超时 |
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
| `AGGREGATOR_URL` | 通常不改 | 中央聚合服务默认使用 `https://codex-monitor.aiof.top`；自建部署可改为自己的地址 |
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

项目中央聚合器：

```bash
AGGREGATOR_URL="https://codex-monitor.aiof.top"
```

程序在根地址后自动追加 `/api/collector/snapshot`，也可以直接填写完整上报端点。采集器
上传会显式绕过进程继承的 `HTTP_PROXY`、`HTTPS_PROXY` 和 `ALL_PROXY` 等代理配置，
避免工作站代理抖动让远端快照超过 TTL。

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

启用远程上报时，`/healthz` 的 `collector` 字段会包含规范化目标地址、是否绕过代理、
累计尝试/成功/失败次数、连续失败次数、最近尝试/成功/失败时间和最新错误；该字段不含
Bearer Token。采集日志使用 UTC 时间戳，在首次失败、持续失败每 30 秒以及恢复时记录
摘要，便于直接从 `journalctl` 判断断联持续时间。

在 VPS 查询 `/api/servers`，应当能看到该采集器的 `SERVER_ID`。如果采集器断联超过
`REMOTE_TTL`，聚合端会移除它的旧会话。默认 30 秒 TTL 用来限制失联服务器的陈旧
状态；悬浮窗的连续空响应确认则负责吸收短暂的快照抖动。

### 5. 配置和信任 Codex Hook

聚合服务和采集器安装脚本默认都会为 `SERVICE_USER` 执行 Hook 安装。安装器只修改带
Monitor marker 的 command handler，同一 matcher group 内的第三方 handler 和其他配置
都会保留。配置损坏、根节点类型错误或 `hooks` 结构错误时会拒绝写入，不会用空配置覆盖
原文件。也可以手动安装：

```bash
./bin/codex-monitor-install-hooks
```

默认安装的事件固定为：

```text
UserPromptSubmit
Stop
```

`SessionStart`、`PreToolUse` 和 `PostToolUse` 都不是三态正确性的依赖。旧版 Monitor
安装的 `SessionStart` 会在下次运行安装器时移除。仅在排查工具生命周期时临时启用后两者：

```bash
./bin/codex-monitor-install-hooks --include-tool-events
```

工具诊断也只保存 `session_id`、`turn_id`、`tool_name` 和 `tool_use_id` 等白名单元数据，
不保存 tool input、tool response、prompt、assistant 正文或 transcript 路径。恢复默认低频
模式时重新运行不带该选项的安装命令；它会精准移除旧 Monitor 工具 Hook。

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

检查安装状态而不修改文件：

```bash
./bin/codex-monitor-install-hooks --check
```

检查会报告配置是否有效、默认事件是否齐全、命令中的仓库路径是否仍存在、是否残留非默认
Monitor 工具事件，以及 `~/.codex/config.toml` 是否明确设置了
`[features].hooks = false`。显式关闭只会被报告，安装器不会把它改回 `true`。退出码 `0`
表示配置当前且未显式关闭，`1` 表示未安装、陈旧或关闭，`2` 表示配置损坏或检查错误。
Codex 是否已信任命令不能从外部可靠确认，因此检查结果中的 trust 状态始终是 unknown，
仍需在 Codex 内执行 `/hooks`。

安装配置后，在每个已经打开或新打开的 Codex CLI 中执行：

```text
/hooks
```

按 Codex 提示 review/trust 新 Hook。这个信任步骤不能由 systemd 安装脚本绕过。

安装和升级采用事务式写入：存在旧配置时先生成同目录的 `hooks.json.bak`，再写入同目录
临时文件、flush/fsync 并原子替换。备份失败或写入失败会中止更新；内容无变化时不会重写
文件或改变 mtime，避免无意义地使 trust hash 失效。需要回滚时先确认备份可解析，再恢复：

```bash
python3 -m json.tool ~/.codex/hooks.json.bak >/dev/null
cp ~/.codex/hooks.json.bak ~/.codex/hooks.json
python3 -m json.tool ~/.codex/hooks.json >/dev/null
```

恢复或任何实际内容变化后，都要重新执行 `/hooks` review/trust。

检查 Hook 是否产生事件：

```bash
tail -f ~/.local/state/codex-cli-monitor/hooks.jsonl
```

新记录使用 schema v2，并保存稳定的 `session_id`/`turn_id`；schema v1 历史记录继续兼容
读取。日志使用 `0600` 权限、跨进程短时 advisory lock 和单次 `os.write` 追加。活动文件
默认到 8 MiB 后轮转，保留 `hooks.jsonl.1`、`hooks.jsonl.2` 两代，因此正常事件日志约束
在 24 MiB 附近；可通过 `CODEX_MONITOR_HOOK_LOG_MAX_BYTES` 调整单代阈值。读取器只反向
读取所需尾部，跳过 NUL、截断尾行和无效 JSON，不自动修改历史日志。Hook 内部的 stdin
损坏、payload 超限、锁竞争、只读目录、磁盘写入或轮转错误都 fail-open：快速返回 0、
不输出正文，也不会失败、阻塞或改变 Codex turn。

本机 `/healthz` 的 `hooks.installation` 给出安装、路径、显式关闭和 trust-unknown 状态，
`hooks.runtime` 给出最近事件时间、schema 版本、有效/损坏行数、尾读字节数、日志大小、
轮转代数、事件模式以及非敏感的 stdin/写入错误计数和最近诊断。CLI `--json` 的
`hook_health` 字段提供相同信息。它们不包含消息正文、Token 或 transcript 路径。

如果移动了仓库目录，必须重新运行对应安装脚本或 `codex-monitor-install-hooks`，因为 Hook 命令中记录了仓库的绝对路径。随后运行 `--check` 并重新 `/hooks` trust。

同一台机器如果有多个 Linux 用户分别运行 Codex，需要为每个用户分别安装 Hook 和采集服务，并使用不同的 systemd unit 名称、PID 文件和本机 API 端口；默认模板针对一个 Codex 用户设计。

### 6. 配置 Windows 悬浮窗

Windows 前端发布目录只包含两个文件：

```text
dist/CodexMonitorWidget-win-x64/CodexMonitorWidget.exe
dist/CodexMonitorWidget-win-x64/CodexMonitorWidget.ini
```

`dist` 被 Git 忽略，因此全新 clone 不会自带发布目录。可以从已经构建的机器复制该目
录，或者按照后文“构建 Windows x64 exe”步骤在 Linux/WSL 构建。构建过程会从配置
模板生成同目录的 `CodexMonitorWidget.ini`。

用记事本打开配置文件：

```powershell
notepad .\dist\CodexMonitorWidget-win-x64\CodexMonitorWidget.ini
```

配置格式：

```ini
[CodexMonitorWidget]
ApiUrl=https://codex-monitor.aiof.top/api/sessions
ApiToken=填写 VPS 的 API_READ_TOKEN
```

字段说明：

| 配置 | 说明 |
|---|---|
| `ApiUrl` | 聚合读取地址，建议包含 `/api/sessions`；只填写主机地址时程序会自动补全路径 |
| `ApiToken` | 必须等于 VPS 的 `API_READ_TOKEN`；API 未启用读取鉴权时可以留空 |

保存后，直接双击：

```powershell
.\dist\CodexMonitorWidget-win-x64\CodexMonitorWidget.exe
```

程序始终从 exe 所在目录读取固定文件名 `CodexMonitorWidget.ini`。配置文件包含明文只读
Token，应只交给需要运行悬浮窗的 Windows 用户。前端具有单实例保护；重复双击不会出
现多个悬浮窗。

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
./bin/codex-monitor-install-hooks
./bin/codex-monitor-install-hooks --check
sudo systemctl restart codex-monitor-aggregator.service
sudo systemctl status codex-monitor-aggregator.service
```

采集服务器：

```bash
cd /path/to/codex-cli-monitor
git pull --ff-only
./bin/codex-monitor-install-hooks
./bin/codex-monitor-install-hooks --check
sudo systemctl restart codex-monitor-collector.service
sudo systemctl status codex-monitor-collector.service
```

Windows 悬浮窗：退出正在运行的旧程序，保留现有 `CodexMonitorWidget.ini`，用重新构
建的 `CodexMonitorWidget.exe` 覆盖旧文件后再次双击。只有配置字段发生变化时才需要
同步新的 INI 模板。

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

卸载 Windows 悬浮窗：从右键菜单退出程序，然后删除
`CodexMonitorWidget.exe` 和 `CodexMonitorWidget.ini`。程序没有安装 Windows Service
或计划任务。

删除 systemd 服务不会自动删除 `~/.codex/hooks.json` 中的 Monitor Hook。使用安装器卸载
只会移除 Monitor handler，并保留第三方 Hook 和其他用户字段：

```bash
./bin/codex-monitor-install-hooks --uninstall
```

卸载发生实际配置变化后仍需在 Codex 中执行 `/hooks` 审查。事件历史默认保留用于诊断；
确认不再需要后可单独归档 `hooks.jsonl` 及其轮转代，不必编辑 `hooks.json`。

### 10. 常见问题

#### 聚合 API 返回 401

- Windows `CodexMonitorWidget.ini` 中的 `ApiToken` 或 curl 使用的 Token 必须等于 VPS
  的 `API_READ_TOKEN`。
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
curl --noproxy '*' https://codex-monitor.aiof.top/healthz
```

`/healthz` 中 `collector.healthy=false` 或 `collector.consecutive_failures` 持续增长时，结合
最近的 `last_error` 和带时间戳的服务日志排查。常见原因包括写入 Token 不一致、URL
错误、防火墙阻止连接、HTTPS 证书无效或服务器 ID 重复。采集器本身不使用环境代理，
因此无需修改系统级代理即可验证直连链路。

#### 状态不更新、没有会话行或结果不准确

- 确认 systemd 服务的 `SERVICE_USER` 与运行 Codex 的用户一致。
- 运行 `./bin/codex-monitor-install-hooks --check`，修复未安装、陈旧路径、损坏配置或显式关闭。
- 在 Codex 中执行 `/hooks` 并确认 Hook 已受信任。
- 查看 `curl http://127.0.0.1:8765/healthz` 中的 `hooks.installation` 和 `hooks.runtime`。
- 检查 Hook 日志是否更新；有活动 Codex 但长期无事件时，只能判断“可能未 trust 或被禁用”，不能据此断言具体原因。
- 检查 `~/.codex` 是否属于正确用户。
- 新进程在第一次 `UserPromptSubmit` 前按设计不会显示；只看到进程而没有 Hook 事件不是故障状态行。
- 确认 Codex session 文件名包含 Hook 记录的 `session_id`。Goal 自动续跑没有提交 Hook
  时，确认新 session 文件仍由同一 Codex PID 打开且包含结构化 `task_started`；终端读取
  器不会用同目录时间接近度猜测其他文件。
- 如果移动过仓库，重新安装 Hook。

#### SSH 已断开但 Codex 会话仍显示

监控会自动忽略 TTY 已删除、终端前台进程组失效或会话 leader 消失的已确认终端孤儿。
但是网络突然中断时，Linux 可能仍把旧 SSH TCP 连接保留为 `ESTABLISHED`，此时
`sshd`、shell 和 Codex 进程树都确实还存在；监控不能仅按空闲时间隐藏它，否则会误伤
正常挂着等待输入的 Codex。

对于经常通过 SSH 运行 Codex 的服务器，建议让 sshd 主动探测客户端。以下配置会在客
户端连续约 60 秒无响应后关闭 SSH 会话，使 Codex 收到终端断开并被回收；请先保持另一
条 SSH 连接以便配置错误时恢复：

```bash
sudo install -d -m 0755 /etc/ssh/sshd_config.d
printf '%s\n' \
  'ClientAliveInterval 30' \
  'ClientAliveCountMax 2' \
  | sudo tee /etc/ssh/sshd_config.d/90-codex-monitor-client-alive.conf >/dev/null
sudo sshd -t
sudo systemctl reload ssh.service
sudo sshd -T | grep -E '^(clientaliveinterval|clientalivecountmax) '
```

部分发行版的服务名是 `sshd.service`。配置只影响之后建立的 SSH 连接；配置前已经形成
的黑洞连接仍需等待系统 TCP keepalive 或清理一次对应 SSH 会话。

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
- 定期轮换 Token；轮换后更新 Linux 服务环境文件以及 Windows
  `CodexMonitorWidget.ini`，并重启对应程序。

聚合结果中的每个 session 包含 `server_id`、`server_name`、`server_boot_id`、跨服务器唯一的 `session_key` 和 `server_observed_at`。不同服务器上相同的 PID 或相同目录不会被当作同一会话。

## Windows 悬浮窗

Windows 前端在 `windows/CodexMonitorWidget`。它是一个轻量原生 Win32 小型矩形
桌面悬浮窗，同一 Windows 登录会话内只允许启动一个实例；如果已经运行，再次启动
exe 会直接退出，不会打开第二个悬浮窗。它会轮询 `/api/sessions`，并按目录分组显示
无表头表格：每行第一列是
目录名，第二列是该目录下一个或多个带柔化边缘的 Codex 进程状态圆点。它不依赖
.NET Runtime 或 Electron。

纯英文目录名继续以小写 `o` 字形的实际黑框做视觉垂直居中；包含中文的目录名和中英
混合目录名则使用 Windows 字体回退后实际渲染出的文字墨迹边界居中，使中文文字中心
与右侧状态圆点中心对齐。墨迹测量结果只在目录行、空状态或显示字号变化时更新，不会
在收展和呼吸动画的每一帧重复测量。

每行最左侧使用彩色竖条区分服务器，目录名前不再添加服务器名前缀。颜色从象牙白、洋
红、琥珀金、亮紫和淡薰衣草色组成的高分离度预设色板中选择，避开状态标识使用的蓝色、
绿色、红色及相近颜色。色板中允许保留彼此接近的候选色，但按服务器名称和 ID 排序后，
相邻服务器的 RGB 距离至少为 240，适合区分深色背景上的 3 像素细彩条；普通刷新期间颜
色保持稳定，只有新增、移除服务器造成新的相邻低对比组合时才会重选必要的颜色。该服
务器的全部会话消失后会释放颜色，之后重新出现时可以获得新的随机颜色。悬浮窗不再绘
制单独的灰
色左边框，服务器彩条贴住窗口左缘并作为每行的左边框，首末行彩条与上下边框无间隙。

尚未收到首次后端响应、后端确认返回空会话列表，或者请求失败且当前没有可显示会话时，
悬浮窗会显示独立的单行空状态，而不是只剩三条边框的空矩形。三种情况分别显示“正在连
接”“暂无会话”和“连接失败”，并使用不占用蓝、绿、红会话状态色的中性左侧强调条与
柔化指示点。空状态同样支持左右贴边收纳：文字渐隐，指示点平滑变为窄竖向胶囊，左侧
强调条始终保留。已有会话可见时，悬浮窗需要连续收到 6 次成功空响应才会清空；请求
失败或重新收到非空响应会重置计数，避免远端 TTL 短暂抖动造成整台服务器闪消。

圆点颜色：

- 带呼吸光晕的蓝色：`运行中`
- 绿色：`成功`
- 红色：`失败`

悬浮窗始终置顶，会按目录名宽度、目录行数和每行圆点数量动态调整大小，不为目录名
预留固定大宽度，可以拖动位置并在下次启动时恢复位置。悬浮窗会保持在屏幕工作区内；
如果动态变宽或变高导致越界，会自动贴到对应边缘。右键菜单中的“贴边收纳”选项勾
选时，悬浮窗贴在左侧或右侧后，鼠标移出 1 秒会动态收纳，只折叠目录名，服务器彩色
标识条继续保留，状态圆点平滑变形为更节省宽度的竖向胶囊条；运行中的蓝色竖条继续
显示呼吸光晕。鼠标移入会动态展开并可打断正在进行的收纳动画，状态条同时恢复为圆
点。完全收纳后，服务器彩条到首个状态条、相邻状态条之间、末个状态条到右边框的三
处水平间距完全一致；取消勾选后不会自动收纳。鼠标移到状态标识上会
显示 PID、状态、目录和启动时间。右键点击悬浮窗会打开菜单，可以调整显示大小、打开
关于页面或退出程序。

连接多服务器聚合服务时，悬浮窗按服务器和目录共同分组，通过每行左侧的彩色竖条区
分服务器，目录名不添加服务器名前缀，悬停详情仍会显示服务器。目录行先按服务器名
称固定排序（名称相同时按服务器 ID），同一服务器内再按每行最早进程启动时间沿用现
有排序；行内状态圆点仍按进程启动时间排序。

Windows 发布目录固定使用一个 exe 和一个同目录配置文件。编辑：

```powershell
notepad .\dist\CodexMonitorWidget-win-x64\CodexMonitorWidget.ini
```

写入聚合 API 地址和读取 Token 后，直接双击同目录的 `CodexMonitorWidget.exe`。程序不
需要 PowerShell、环境变量、Windows Service 或计划任务安装脚本。

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
  -lwinhttp -lcomctl32 -lshell32 -luser32 -lgdi32 -ladvapi32 -lwinmm
cp windows/CodexMonitorWidget/CodexMonitorWidget.ini.example \
  dist/CodexMonitorWidget-win-x64/CodexMonitorWidget.ini
```

发布目录应当恰好包含：

```text
CodexMonitorWidget.exe
CodexMonitorWidget.ini
```

配置文件示例：

```ini
[CodexMonitorWidget]
ApiUrl=https://codex-monitor.aiof.top/api/sessions
ApiToken=
```

保存配置后双击 `CodexMonitorWidget.exe`。为了兼容旧用法，如果同目录没有 INI，程序
仍会读取 `CODEX_MONITOR_API_URL` 和 `CODEX_MONITOR_API_TOKEN`；第一个命令行参数仍可
覆盖 API URL。

命令行兼容示例：

```powershell
.\CodexMonitorWidget.exe http://127.0.0.1:8765
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
