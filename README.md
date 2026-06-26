# codex-cli-monitor

`codex-cli-monitor` 是一个用于观察本机 Codex CLI 运行状态的小工具。它的目标是低侵入地显示当前打开了多少个 Codex 会话，并把每个会话归类为少量可直接使用的状态。

## 功能

- 扫描当前系统里的 Codex CLI 进程，显示会话数量、PID、TTY、工作目录、运行时长等信息。
- 根据 Codex hooks、进程树、CPU 变化、网络连接和 Codex 本地状态文件，判断会话状态。
- 区分“确定事实”和“推断状态”，不会把推断结果当成 Codex 内部真实状态。
- 支持 JSON 输出，方便接入脚本或面板。
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

指定 Codex 本地状态目录：

```bash
PYTHONPATH=src python3 -m codex_cli_monitor --codex-home ~/.codex
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
