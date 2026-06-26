# codex-cli-monitor

`codex-cli-monitor` 是一个用于观察本机 Codex CLI 运行状态的小工具。它的目标是低侵入地显示当前打开了多少个 Codex 会话，并尽量判断每个会话可能正在做什么。

## 功能

- 扫描当前系统里的 Codex CLI 进程，显示会话数量、PID、TTY、工作目录、运行时长等信息。
- 根据进程树、CPU 变化、网络连接和 Codex 本地状态文件，推断会话状态。
- 区分“确定事实”和“推断状态”，不会把推断结果当成 Codex 内部真实状态。
- 支持 JSON 输出，方便接入脚本或面板。
- 提供可选的同名 `codex` shim，用来记录启动元数据后再透明执行真正的 Codex CLI。

## 大概原理

默认方式是 sidecar 监控：工具独立运行，不修改 Codex，也不要求改变正常使用习惯。它主要读取这些只读信号：

- `/proc` 里的进程、父子进程、命令行、TTY、当前工作目录和 CPU 时间。
- 进程持有的网络连接，用作远程 API 请求的辅助判断。
- `$CODEX_HOME` 下的本地状态文件元数据，例如 session JSONL 文件路径、大小、修改时间，以及头尾结构化事件类型。
- 可选 shim 写入的启动记录。

为了减少误判，远程 API 进行中的判断不会只依赖长连接；它需要近期 Codex session 文件活动和网络连接共同支撑。监控不会输出 session JSONL 的消息正文。

推断状态包括：

- `waiting_user_likely`：可能正在等待用户输入。
- `api_inflight_likely`：可能正在等待远程 API 响应。
- `tool_running_likely`：可能正在运行本地命令、测试或构建。
- `active_likely`：有活动，但无法更具体分类。
- `unknown`：信号不足或互相矛盾。

这些状态都会带有置信度和证据，属于“可能如此”，不是 Codex 内部事件的精确声明。

## 使用方法

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
