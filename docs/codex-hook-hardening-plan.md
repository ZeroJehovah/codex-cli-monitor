# Codex Hook 监控加固开发计划

## 1. 目的

本计划用于系统吸收 Catrace 在 Hook 安装可靠性、本地事件接收和故障隔离方面的成熟做法，并结合当前 Codex 官方 Hook 契约，完善 `codex-cli-monitor` 的生命周期观测。目标不是复制 Catrace 的通知产品，而是把适用于低侵入监控的工程实践全部落实：配置修改可恢复、Hook 永不干扰 Codex、事件以稳定标识关联、日志有界且并发安全、安装状态可诊断、升级和卸载可验证。

当前基线已经具备 sidecar 进程发现、Hook 生命周期、Codex session JSONL 分析、三态展示、多服务器采集聚合和原生 Windows 前端。本轮只加强 Hook 子系统及其与 session 状态的绑定，不改变用户正常运行 `codex` 的方式，不传输 prompt、assistant 正文或 tool input。

## 2. 调研依据与已确认事实

### 2.1 Catrace 可借鉴部分

参考版本：`lanxiuyun/Catrace@b8ff81afccc4c70252e829718904c95dd996581e`。

- 安装器用 marker 识别自己的条目，只同步或删除自己的 Hook，保留用户其他配置。
- 修改配置前创建备份，写入采用临时文件加原子替换。
- Hook 中继设置 stdin 和网络短超时，失败静默退出，不让辅助功能阻塞 Agent。
- 高频事件与低频用户提醒分开；Codex 默认只安装 `UserPromptSubmit`、`Stop`。
- 安装器支持检测、同步更新和卸载，而不是只做一次性追加。
- Windows/WSL 命令分别处理，并支持 Codex 的 `commandWindows` 字段。
- 集中接收端让并发 Hook 不直接竞争写同一个状态文件。

### 2.2 Codex 当前官方契约

依据当前 Codex Hooks 文档：<https://learn.chatgpt.com/docs/hooks.md>。实施前若 Codex 版本或文档契约发生变化，应重新核对。

- Hooks 默认启用；`features.hooks = false` 是用户明确关闭，不应被安装器覆盖。旧的 `codex_hooks` 只是废弃别名。
- 同一事件的多个匹配命令会并发启动；非托管 Hook 变更后必须重新 review/trust。
- 每个 command Hook 从 stdin 收到一个 JSON 对象，公共字段包括 `session_id`、`transcript_path`、`cwd`、`hook_event_name` 和 `model`。
- turn 级事件提供 `turn_id`；工具事件还提供 `tool_name`、`tool_use_id` 等稳定标识。
- `commandWindows` 是受支持的 Windows 命令覆盖字段。
- `async` 虽可解析，但当前不支持真正的异步 command Hook。
- transcript 格式不是稳定 Hook 接口；可作为兼容性 sidecar 信号，不能替代 Hook 的稳定字段。
- `Stop` 退出码为 0 且无 stdout 是有效成功结果。

### 2.3 本项目实测基线

在 2026-07-28、Codex CLI 0.145.0 的本机部署中确认：

- Hook 日志约 19.5 MB、94,628 行，其中约 90% 以上是 `PreToolUse`/`PostToolUse`。
- 工具 Hook 为降低阻塞而丢弃 stdin，导致 `session_id`、`turn_id`、`tool_name`、`tool_use_id` 均未记录；高频记录价值有限。
- Hook 日志出现过包含 NUL 的损坏行；当前多个进程直接追加同一 JSONL，缺少明确的跨进程写入协议。
- 冷加载约 19.5 MB 日志需要约 0.12 秒，峰值内存约 59 MB；读取端虽然只解析末尾 2000 行，但先读入整个文件。
- `UserPromptSubmit` 已记录 `session_id`，但 session JSONL 绑定仍主要依据 cwd 与时间接近度，没有把 `session_id` 用作硬匹配。
- Hook 安装器遇到损坏的 `hooks.json` 会退化为空配置并覆盖原文件，且当前没有备份、原子替换、检查和卸载命令。

## 3. 总体设计原则

1. **低频事件构成默认正确性路径。** 默认安装 `UserPromptSubmit`、`Stop`；新会话在首次提交前不显示，工具事件只作为显式启用的增强诊断。
2. **稳定 ID 优先。** 关联顺序为 `turn_id` 精确匹配、`session_id` 精确匹配、PID/启动时间、时间窗口兜底。ID 冲突时保守拒绝绑定，不做模糊覆盖。
3. **Hook 全链路 fail-open。** stdin 无效、目录不可写、锁失败、接收端不可达或日志轮转失败，都不得改变 Codex turn 结果；Hook 应快速、无 stdout、以 0 退出。
4. **配置写入事务化。** 配置不可解析时停止并返回可操作错误；只修改带 monitor marker 的条目；备份、同目录临时文件、flush/fsync、原子替换。
5. **状态数据最小化。** 只保留关联和状态判断所需元数据，不保存 prompt、assistant 消息正文、tool input、tool response、模型输出或 Bearer token。
6. **sidecar 始终可用。** Hook 未安装、未信任、显式关闭、接收失败或版本不支持时，现有 `/proc` 和本地状态文件观察继续工作。
7. **兼容旧日志。** 新 schema 必须能读取 schema v1；迁移不要求重写历史日志。
8. **有界资源。** Hook 日志必须限制磁盘占用，读取必须与文件总大小近似无关，长期运行不能无限增长。

## 4. 目标事件模型

Hook 持久化记录采用 schema v2，建议字段如下：

```json
{
  "schema_version": 2,
  "event": "user_prompt_submit",
  "timestamp": 1785228057.525,
  "pid": 24945,
  "ppid": 25947,
  "cwd": "/workspace/project",
  "session_id": "019f...",
  "turn_id": "...",
  "tool_name": null,
  "tool_use_id": null,
  "hook_source": null
}
```

字段纪律：

- `session_id`：所有能提供该字段的事件都保存。
- `turn_id`：turn 级事件都保存，是 turn 绑定的首选键。
- `tool_name`、`tool_use_id`：只在启用工具诊断时保存；不保存输入和响应。
- `ppid`：继续作为当前 Linux 实现的进程关联信号，但不把 shell `$PPID` 当跨平台稳定协议。
- `hook_event_name` 用于校验安装命令传入的事件是否和 payload 一致；不一致时记录安全诊断并以 payload 为准或丢弃该条，具体策略由测试固化。
- 不写 `transcript_path`，除非未来仅保存经过规范化的相对标识且有明确必要；现阶段 session 文件由 sidecar 自行发现。

Hook session 状态按 `(session_id, turn_id)` 聚合；缺少 ID 的旧事件再按 `(cwd, codex_pid)` 聚合。工具活跃状态优先按 `tool_use_id` 集合计算，避免并发工具或丢失 Post 事件造成简单计数漂移。

## 5. 分阶段实施

### 阶段 0：基线夹具与回归保护

目标：先固化现状和故障样本，避免安全重构改变三态语义。

任务：

- 为损坏 `hooks.json`、非对象根节点、错误 `hooks` 类型、已有第三方 Hook、已有旧 monitor Hook 建立安装器夹具。
- 为包含 NUL、截断尾行、交错 JSON、超大历史文件和 schema v1/v2 混合日志建立读取夹具。
- 建立同 cwd 双进程、同 session 多 turn、并行工具、缺失 PostToolUse、Stop 先后顺序等绑定用例。
- 记录当前 CLI/API JSON 的兼容基线，确保新增诊断字段不改变顶层三态。
- 增加性能基准脚本或测试辅助函数，覆盖大文件尾读时间和内存上限；基准不应依赖机器绝对速度，可用读取字节数和宽松时间阈值双重验收。

主要文件：

- `tests/test_install_hooks.py`
- `tests/test_hooks.py`
- `tests/test_hook_state.py`
- `tests/test_monitor.py`
- 新增 `tests/fixtures/` 下的最小脱敏样本（如确有必要）

验收：现有测试全绿；新增测试在旧实现上能准确暴露计划要解决的问题。

### 阶段 1：安装器安全、可诊断、可卸载

目标：消除覆盖用户配置和留下陈旧 Hook 的风险。

任务：

- 将读取结果区分为“不存在”“有效”“无效”，无效配置禁止覆盖。
- 验证根节点和 `hooks` 结构；只删除 marker 命中的 monitor handler，保留同 matcher group 内的其他 handler。
- 写入前创建时间稳定或单份 `.bak` 备份；备份失败默认中止配置修改，除非明确设计为可安全继续并有测试依据。
- 使用同目录临时文件、保留合理权限、flush/fsync、`os.replace()` 原子替换；异常时清理临时文件并保留原配置。
- 在实际内容没有变化时不重写文件，避免无意义地使 Hook trust hash 失效。
- 增加 CLI：`--check` 输出配置存在、结构有效、monitor 事件齐全、命令路径有效、是否显式 `hooks=false`、是否可能需要 `/hooks` review；`--uninstall` 只移除 monitor 条目，空事件和空 `hooks` 容器按保守规则清理。
- `--install`/默认安装输出新增、更新、未变、错误的事件清单，并明确提示内容变化后需要 `/hooks` review/trust。
- 检查 `config.toml` 只用于诊断显式 `features.hooks=false`；不主动写 true，也不迁移用户配置。
- 为将来原生 Windows collector 生成 `commandWindows` 留下平台抽象和测试，但本阶段不宣称 Windows 后端已受支持。
- 更新 README 的安装、检测、信任、升级、卸载和恢复备份说明。

主要文件：

- `src/codex_cli_monitor/install_hooks.py`
- `bin/codex-monitor-install-hooks`
- `tests/test_install_hooks.py`
- `README.md`

验收：

- 无效配置字节级保持不变。
- 任一写入故障后原文件仍可解析，备份可恢复。
- 连续安装幂等；命令路径变化会精准同步；无变化不更新 mtime。
- 卸载后第三方 Hook 与用户其他字段字节语义不变。
- `--check` 在未安装、已安装、路径陈旧、显式关闭和损坏配置下均返回明确状态与合适退出码。

### 阶段 2：Hook 执行 fail-open 与稳定 ID 采集

目标：在不明显增加 Hook 延迟的前提下采集官方稳定关联字段。

任务：

- 对 `UserPromptSubmit`、`Stop` 完整读取小型 stdin JSON，只提取白名单字段；继续兼容读取旧 `SessionStart` 记录。
- 将 `turn_id` 加入 `HookEvent`、JSONL、加载器和 `HookSessionState`；schema 升级到 v2，兼容 v1。
- 将 Hook 入口最外层包成 fail-open：所有解析、目录、写入、锁和编码异常均以 0 退出，不向 stdout 输出。
- 对 stdin 设置合理的最大字节数；超限时排空或安全停止读取，只记录不含正文的诊断计数。不得因 payload 含大 tool output 耗尽内存。
- 校验实际 payload 的 `hook_event_name`，避免安装命令事件名和 stdin 事件错配。
- 默认安装事件缩减为 `UserPromptSubmit`、`Stop`。
- 增加显式工具诊断开关，例如安装器 `--include-tool-events`；仅在开启时安装 `PreToolUse`/`PostToolUse`。
- 工具诊断模式读取白名单 `session_id`、`turn_id`、`tool_name`、`tool_use_id`，不读取或保存 tool input/response。先用真实 payload 尺寸测试同步白名单解析是否足够轻；若不能满足延迟门槛，再进入阶段 4B 的集中接收方案。
- 对现有已安装的工具 Hook 提供迁移：默认重新安装会移除旧 monitor 工具条目，并明确告知 trust hash 变化。

主要文件：

- `src/codex_cli_monitor/hooks.py`
- `src/codex_cli_monitor/hook_state.py`
- `src/codex_cli_monitor/install_hooks.py`
- `tests/test_hooks.py`
- `tests/test_hook_state.py`
- `tests/test_install_hooks.py`

验收：

- Hook 写入目录只读、磁盘满、日志锁失败、stdin 损坏或超限时，进程仍快速以 0 退出。
- 默认安装不再产生 Pre/PostToolUse 记录。
- 工具诊断模式能按 `tool_use_id` 正确处理并发和乱序，不因简单计数漂移永久显示工具运行中。
- JSONL 中不存在 prompt、assistant 文本、tool input、tool response 或 transcript 正文。
- 低频 Hook 的本机 p95 执行时间设定并达到明确门槛；建议目标小于 20 ms，最终以 CI/目标服务器基线校准。

### 阶段 3：session/turn 精确绑定

目标：让同目录多进程和多 turn 的关联先使用稳定 ID，时间启发式只作兜底。

任务：

- 在 Hook 状态中保存当前和最近终止的 `turn_id`，避免新 turn 覆盖旧 turn 的失败归属。
- `_activity_candidates_for_root()` 先排除明确的 session/turn ID 冲突。
- `_activity_sort_key_for_root()` 增加匹配等级：turn 精确、session 精确、无 ID、明确冲突；明确冲突不得仅因时间接近而胜出。
- `_activity_matches_hook()` 首先检查稳定 ID，只有任一侧缺失 ID 时才进入时间窗口判断。
- 一对一分配继续保留，但优先分配唯一精确匹配；存在多个同级精确候选时输出诊断证据并保守选择或不绑定。
- 将绑定依据以非敏感诊断字段暴露，例如 `binding_method=turn_id|session_id|pid_time|cwd_time` 和置信度，便于排障；不改变前端要求字段。
- Codex session JSONL 没有稳定字段或格式变化时，继续使用既有 sidecar 退化路径，并明确标记局限。
- 结合 `Stop` 的同 turn 终止事件与失败诊断，保证旧 turn 的失败不会污染新 turn，后来的 `task_complete` 也不覆盖同 turn 已确认的 failure。

主要文件：

- `src/codex_cli_monitor/hook_state.py`
- `src/codex_cli_monitor/monitor.py`
- `src/codex_cli_monitor/models.py`
- `src/codex_cli_monitor/codex_state.py`（仅在稳定 ID 提取需要修正时）
- `tests/test_hook_state.py`
- `tests/test_monitor.py`
- `tests/test_codex_state.py`

验收：

- 同 cwd 两个 Codex 进程的 session/turn 状态不串线。
- 时间更近但 ID 冲突的 activity 永远不会覆盖 ID 精确匹配。
- 没有 Hook ID 的旧会话保持现有可用行为。
- 主状态仍只可能是 `运行中`、`成功`、`失败`。

### 阶段 4：并发安全、有界日志与增量读取

目标：长期运行时日志不损坏、不无限增长，监控读取成本不随历史总量线性增加。

先实现较小改造，再用压力测试决定是否需要集中接收端。

#### 4A. 文件协议加固

- 每条 JSON 在内存中一次编码为 UTF-8 bytes，以单次低层 `os.write()` 追加；使用 `O_APPEND|O_CREAT|O_WRONLY`。
- 使用跨进程 advisory lock 或锁文件串行化“检查轮转 + 追加”；锁等待设短上限，失败即 fail-open。
- 为日志设置权限，避免其他本机用户读取会话标识。
- 实施大小轮转，例如活动文件 4–8 MiB、保留 2–3 代；具体默认值通过部署频率和诊断需要确定。轮转必须在锁内完成，旧文件压缩与否以复杂度和 CPU 成本评估。
- 读取端从文件尾反向读取足够的完整行，而不是 `read_text()` 整个文件；跳过 NUL、截断和无效行，并暴露非敏感损坏行计数。
- 缓存键加入 inode/设备号，正确处理 rename 轮转和截断后重建。
- 提供一次性 `--repair-hook-log` 或诊断建议前，先评估是否真的需要；默认读取器容错即可，不自动改用户历史文件。

#### 4B. 条件性集中接收端

仅当 4A 压力测试仍无法满足并发完整性或 Hook 延迟目标时实施：

- 在现有 resident backend 内增加 Unix domain socket 单写者接收，不新增 Node 依赖和固定 TCP 端口。
- Hook 使用短连接发送最小 JSON，连接/写入超时后立即放弃；后端不可用不影响 Codex。
- socket 放在用户 runtime/state 目录，权限 0600；防止其他用户注入虚假状态。
- 后端负责顺序化、轮转、健康计数；Hook 端可选择直接文件 fallback，但需防止重新引入并发损坏。
- 不让 Hook 同步依赖 collector/aggregator 网络，远端仍只接收当前最小快照。

主要文件：

- `src/codex_cli_monitor/hook_state.py`
- `src/codex_cli_monitor/hooks.py`
- `src/codex_cli_monitor/api.py`（仅 4B）
- `src/codex_cli_monitor/cli.py`（诊断/配置项）
- `tests/test_hook_state.py`
- `tests/test_hooks.py`
- `tests/test_api.py`（仅 4B）

验收：

- 至少数十个并发写进程、数万条事件压力测试后，每一行均为完整 JSON，无 NUL 和交错。
- 达到轮转阈值后总占用保持在配置上限附近。
- 读取 100 MiB 历史布局时，实际读取量仅与所需尾部事件和轮转代数相关。
- 轮转、截断、崩溃尾行、权限错误均不会导致 API 服务退出或 Hook 阻塞。

### 阶段 5：健康诊断、可观测性与运维收口

目标：用户能判断“不准”究竟来自未安装、未信任、显式关闭、日志失败还是绑定退化。

任务：

- `/healthz` 增加非敏感 Hook 健康摘要：最近事件时间、schema 版本、有效/损坏行计数、日志大小/轮转代数、最近写入错误计数、默认或工具诊断模式。
- CLI JSON 增加相同诊断信息；普通表格保持简洁，只在异常时输出提示。
- 检测“配置已安装但近期活动会话从未产生 Hook”只能报告为可能未 trust/被禁用，不能声称确定原因。
- README 补齐升级迁移：旧高频 Hook 如何移除、为何需要重新 `/hooks` trust、如何回滚 `.bak`、如何检查 schema v2、如何启用工具诊断。
- 部署模板保持 Hook 本地，不增加 aggregator 依赖；服务更新后验证本机和中央健康端点。
- 更新项目 Overview 和约束，使默认低频、稳定 ID、fail-open、配置事务化、日志有界成为长期要求。

主要文件：

- `src/codex_cli_monitor/api.py`
- `src/codex_cli_monitor/cli.py`
- `src/codex_cli_monitor/models.py`
- `tests/test_api.py`
- `tests/test_cli.py`
- `README.md`
- 本地 `AGENTS.md` 与加密 `.my-prompt`

验收：从全新 clone 能完成安装、trust、检查、运行、升级、卸载和故障恢复；健康信息不暴露 token 或消息正文。

## 6. 明确不做或条件性做

### 明确不做

- 不复制 Catrace 的 Toast、sticky 通知、会话摘要、声音或审批 UI。
- 不读取 transcript 来生成标题或 assistant 摘要。
- 不记录 prompt、assistant 正文、tool input、tool response。
- 不接入阻塞式 `PermissionRequest` 审批，也不允许监控 Hook 改写或阻止工具。
- 不使用 `--dangerously-bypass-hook-trust` 规避 Codex 安全审查。
- 不主动把用户的 `features.hooks=false` 改成 true。
- 不为 Hook 引入 Node、Electron、Tauri 或其他重运行时。
- 不把 Hook 事件同步发送给中央 aggregator。

### 条件性做

- Unix socket 单写者：只有文件协议在压力测试下仍不可靠或延迟不达标时才实施。
- Windows 后端 Hook：只建立配置生成抽象和测试；等项目正式支持 Windows collector 时才交付完整运行链路。
- `SessionEnd`、Subagent 事件、Pre/PostCompact：当前三态监控没有必要，不因“官方支持”就默认安装；未来有明确状态需求再独立设计。
- Plugin 打包 Hook：当前用户级安装器更符合跨项目监控；只有插件分发能实质改善安装与升级时再评估。

## 7. 测试矩阵

| 维度 | 必测场景 |
|---|---|
| 配置 | 不存在、有效空配置、第三方 Hook、旧 monitor Hook、损坏 JSON、错误类型、只读目录、写到一半失败 |
| 安装生命周期 | 初装、重复安装、仓库路径变化、check、卸载、备份恢复、trust hash 无变化 |
| Hook stdin | 空、无效 JSON、超限、缺字段、未知字段、事件名不一致、schema 演进 |
| 事件 | UserPromptSubmit、Stop；兼容旧 SessionStart；可选 Pre/PostToolUse 并发、乱序、缺失 |
| 绑定 | 同 cwd 多 PID、同 session 多 turn、ID 精确、ID 冲突、仅 PID、仅时间、旧日志无 ID |
| 日志 | 并发追加、NUL、截断尾、轮转、inode 替换、超大文件、权限/磁盘错误 |
| 状态 | 新开成功、开放 turn 运行中、Stop 成功、同 turn failure 失败、下一 turn 清除旧失败 |
| 隐私 | 断言日志和 API 中没有 prompt、assistant/tool 正文、transcript 路径和 token |
| 部署 | 本地 resident backend、collector、aggregator 本地观察、远端 snapshot 不变 |

验证命令至少包括：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

涉及 Windows 命令生成或前端共享契约时，继续运行相关 widget 测试与 MinGW 构建；纯后端 Hook 改动不应无故重建 Windows 可执行文件。

## 8. 兼容与迁移策略

- schema v1 日志继续读取；缺少稳定 ID 时维持现有 `(cwd, pid, timestamp)` 兜底。
- schema v2 从部署时自然开始写入，不原地重写旧日志。
- 首次采用默认低频配置时，安装器删除自己的 Pre/PostToolUse 条目，保留所有第三方条目，并提示用户重新 `/hooks` 审查。
- 轮转上线时把现有 `hooks.jsonl` 作为第一代历史文件处理，不删除；达到保留上限后的淘汰规则必须在 README 明示。
- API 新增字段只做向后兼容扩展；现有 Windows widget 继续只依赖 status、cwd、start time 等既有字段。
- 任一阶段无法确认 Codex 新版本契约时，保持已有 sidecar 路径，不通过猜测扩大 Hook 权限或字段读取。

## 9. 提交与发布顺序

每个阶段作为独立、可回滚的开发迭代，建议提交粒度：

1. `test(hooks): add lifecycle hardening fixtures`
2. `fix(hooks): make configuration updates transactional`
3. `feat(hooks): capture stable lifecycle identifiers`
4. `fix(monitor): bind sessions by stable hook identifiers`
5. `perf(hooks): bound and incrementally read event logs`
6. `feat(hooks): expose installation and runtime health`
7. `docs: document hook operations and migration`

每个功能提交前运行针对性测试和全量测试；提交后立即 push。后端逻辑提交后更新本机 resident service；每次完整迭代后更新 `Oracle.Cloud.SG.01` 中央 aggregator，重启服务并验证内外健康端点、MainPID 实际命令和 journal。若阶段更新了 `AGENTS.md`，先单独更新、提交并推送仅包含 `.my-prompt` 的加密约束提交。

## 10. 完成定义

全部阶段完成需同时满足：

- 安装器不会因无效配置覆盖用户文件，安装/检查/同步/卸载均可验证。
- 默认 Hook 只有低频生命周期事件，监控三态准确性不依赖高频工具事件。
- Hook 的任何内部故障都不会阻断、失败或改变 Codex turn。
- Hook 与 session activity 首先按 `turn_id/session_id` 绑定，同 cwd 多进程不串线。
- 日志并发压力下无交错或 NUL，磁盘占用有上限，读取成本不随总历史线性增长。
- 健康诊断能说明实际信号和退化路径，同时不泄露会话正文或凭据。
- sidecar baseline、collector/aggregator、Windows widget 的现有行为均通过回归测试。
- README、部署说明、项目约束和中央运行实例全部与最终实现一致。
