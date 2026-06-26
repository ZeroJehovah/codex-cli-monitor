# codex-cli-monitor

`codex-cli-monitor` 是一个用于观察本机 Codex CLI 运行状态的小工具。它的目标是低侵入地显示当前打开了多少个 Codex 会话，并把每个会话归类为少量可直接使用的状态。

## 功能

- 扫描当前系统里的 Codex CLI 进程，显示会话数量、PID、TTY、工作目录、运行时长等信息。
- 根据 Codex hooks、进程树、CPU 变化、网络连接和 Codex 本地状态文件，判断会话状态。
- 区分“确定事实”和“推断状态”，不会把推断结果当成 Codex 内部真实状态。
- 支持 JSON 输出，方便接入脚本或面板。
- 支持常驻后台 HTTP API，供桌面前端轮询当前 Codex 会话状态。
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

主状态只有四种：

- `未运行`：新会话，或 `/new`、清空、恢复等产生的新 turn 上下文，尚未提交提示词。
- `运行中`：已提交提示词，AI 正在思考、等待 API、执行 MCP、本地工具或其他操作。
- `成功`：从运行中结束，且最近一轮结果完成成功。
- `失败`：从运行中结束，且最近一轮出现 API/模型错误，或被手动 Ctrl+C 中断。

JSON 里仍保留 `inferred_status` 诊断字段，里面可能出现 `waiting_user_likely`、`api_inflight_likely`、`tool_running_likely` 等内部推断值，用来解释证据和置信度；表格和顶层 `status` 只显示上面的四种主状态。

## 使用方法

先安装低侵入 hooks，用来记录 Codex turn/tool/stop 生命周期事件：

```bash
./bin/codex-monitor-install-hooks
```

安装后，在每个正在运行或新打开的 Codex CLI 里执行 `/hooks`，按提示 review/trust 新 hook。这个步骤是 Codex 的安全机制。

信任后，监控会优先使用 hook 事件判断状态：`SessionStart` 视为 `未运行`，用户提交后到 `Stop` 前视为 `运行中`，`Stop` 后结合 session JSONL 末尾事件判断 `成功` 或 `失败`。同一工作目录下的多个 Codex 进程会按 Codex PID 和进程启动时间隔离，避免新会话继承旧会话的完成状态。没有 hook 事件的旧会话会继续使用 sidecar 信号降级推断。

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

## Windows 悬浮窗

Windows 前端在 `windows/CodexMonitorWidget`。它是一个轻量原生 Win32 小型矩形
桌面悬浮窗，会轮询 `/api/sessions`，并按目录分组显示无表头表格：每行第一列是
目录名，第二列是该目录下一个或多个 Codex 进程状态圆点。它不依赖 .NET Runtime
或 Electron。

圆点颜色：

- 灰色：`未运行`
- 蓝色：`运行中`
- 绿色：`成功`
- 红色：`失败`

悬浮窗始终置顶，会按目录名宽度、目录行数和每行圆点数量动态调整大小，不为目录名
预留固定大宽度，可以拖动位置。鼠标移到圆点上会显示 PID、状态、目录和启动时间。
右键点击悬浮窗会打开菜单，可以选择退出程序。

构建 Windows x64 exe：

```bash
x86_64-w64-mingw32-gcc -Os -s -DUNICODE -D_UNICODE \
  windows/CodexMonitorWidget/src/main.c \
  -o dist/CodexMonitorWidget-win-x64/CodexMonitorWidget.exe \
  -mwindows -municode \
  -lwinhttp -lcomctl32 -lshell32 -luser32 -lgdi32
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
