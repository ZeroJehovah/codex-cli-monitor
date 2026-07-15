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

多服务器模式使用同一个程序承担两个角色：

- 采集器运行在每台 Codex 服务器上，继续读取本机 `/proc`、Hook 日志和 Codex session 文件，并异步推送最小状态快照。
- 聚合服务通常运行在 VPS 上，接收远端快照，同时照常采集 VPS 本机的 Codex 状态，最终向前端返回合并结果。

Hook 始终只写本地文件，不直接访问 VPS，因此 VPS 离线或网络抖动不会阻塞 Codex。建议让所有设备通过 Tailscale 或 WireGuard 互通，不要直接把未受防火墙保护的端口暴露到公网。

后端是纯 Python，不需要编译；VPS 和采集服务器只需要 Python 3.10 或更高版本以及 systemd。clone 仓库后，在 VPS 安装聚合系统服务：

```bash
install -m 700 start-server.sh.example start-server.sh
vim start-server.sh
./start-server.sh
```

`start-server.sh` 中需要填写服务器标识、监听地址、前端读取 Token 和采集器写入 Token。脚本会安装本机 Hook、生成 `codex-monitor-aggregator.service`，将 Token 写入权限为 `0600` 的 `/etc/codex-cli-monitor/aggregator.env`，然后通过 systemd 启动并设置开机自启。服务会使用脚本中的 `SERVICE_USER` 运行；该用户必须与实际运行 Codex 的 Linux 用户一致。安装后仍需在已打开的 Codex 会话中执行 `/hooks` 完成信任确认。

查看聚合服务：

```bash
sudo systemctl status codex-monitor-aggregator.service
sudo journalctl -u codex-monitor-aggregator.service -f
```

监听地址最好填写 VPS 的 Tailscale/WireGuard 地址。如果必须通过公网访问，应在 Caddy、Nginx 等反向代理后提供 HTTPS，并配置防火墙和限流。

在其他 Linux 服务器安装采集系统服务：

```bash
install -m 700 start-collector.sh.example start-collector.sh
vim start-collector.sh
./start-collector.sh
```

脚本会安装本机 Hook，并生成、启用 `codex-monitor-collector.service`。采集器默认每 0.5 秒发送一次当前快照；聚合服务默认 5 秒没有收到更新就从活动结果中移除该服务器的旧会话。安装后仍需在已打开的 Codex 会话中执行 `/hooks` 完成信任确认。

```bash
sudo systemctl status codex-monitor-collector.service
sudo journalctl -u codex-monitor-collector.service -f
```

查询带读取鉴权的聚合 API：

```bash
curl \
  -H "Authorization: Bearer $CODEX_MONITOR_API_TOKEN" \
  http://100.64.0.10:8765/api/sessions
```

聚合结果中的每个 session 会增加 `server_id`、`server_name`、`server_boot_id`、跨服务器唯一的 `session_key` 和 `server_observed_at`。顶层还会返回 `server_count` 和 `servers`。不同服务器上相同的 PID 或相同目录不会被合并为同一会话。

各服务器应启用 NTP 时间同步；跨服务器的进程开始时间和界面排序仍以各机器报告的系统时间为准。

仓库提交 `.example` 模板，但忽略没有 `.example` 后缀的本地脚本。因此 `start-server.sh`、`start-collector.sh` 和 `start-widget.ps1` 可以保存真实 Token，不会被 Git 提交。

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
