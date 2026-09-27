# Windows 悬浮窗编译说明

## 更新内容

✅ **已更新**: WebSocket 端点现在与 HTTP API 共用端口，并使用 `/ws` 路径
- 现在 WebSocket 与 HTTP API 使用同一端口
- 自动从 API URL 提取主机和端口信息
- 添加 `/ws` 路径到 WebSocket 连接

## 编译步骤（推荐：一条命令）

在项目根目录执行：

```bash
windows/CodexMonitorWidget/build-widget.sh
```

脚本不需要 root，也不依赖系统预装的 mingw-w64：它会用 `apt-get download` 拉取 Ubuntu
的 mingw-w64 相关 deb 包，再用 `dpkg -x` 解包到私有前缀（默认 `/tmp/codex-mingw`，`debs/`
放 deb 包，`root/usr` 放解包后的工具链；可用 `CODEX_MONITOR_MINGW_CACHE` 指向持久
目录），然后带着 `CC`/`BINUTILS_DIR`/`MINGW_INCLUDE_DIR` 调用本目录的 `Makefile`。
缓存命中时不会重复下载。

输出：

```text
dist/CodexMonitorWidget-win-x64/CodexMonitorWidget.exe
dist/CodexMonitorWidget-win-x64/CodexMonitorWidget.ini   # 目录里已有 INI 时保留原文件
```

`windows/CodexMonitorWidget/CodexMonitorWidget.exe` 是同一份产物的副本，两者必须字节一致。

## 编译步骤（备选：系统已装 mingw-w64）

如果所在环境已经安装交叉编译工具链（例如 `sudo apt-get install -y mingw-w64`），可以
直接使用 Makefile：

```bash
make -C windows/CodexMonitorWidget
```

需要覆盖编译器或 binutils 路径时：

```bash
make -C windows/CodexMonitorWidget \
  CC=/path/to/x86_64-w64-mingw32-gcc-posix \
  BINUTILS_DIR=/path/to/bin \
  MINGW_INCLUDE_DIR=/path/to/include
```

## 配置文件

编辑 `dist/CodexMonitorWidget-win-x64/CodexMonitorWidget.ini`：

```ini
[CodexMonitorWidget]
ApiUrl=https://codex-monitor.aiof.top/api/sessions
ApiToken=你的-API-令牌
```

## 使用说明

1. 双击 `CodexMonitorWidget.exe` 启动悬浮窗
2. 程序会自动连接到配置的 API 地址
3. WebSocket 将自动连接到 `ws://host:port/ws` 实现实时更新
4. 悬浮窗显示所有服务器上的 Codex CLI 会话状态

## 部署到 Windows

发布物只有两个文件：`CodexMonitorWidget.exe` 和同目录的 `CodexMonitorWidget.ini`。
更新已有安装时，只需要把新编译出的 exe 覆盖到 INI 旁边，再重新启动悬浮窗；INI 里的
API 地址和 Bearer Token 不需要改动。

## WebSocket 连接示例

如果你的 API URL 是：
- `https://codex-monitor.aiof.top/api/sessions`

那么 WebSocket 将连接到：
- `wss://codex-monitor.aiof.top/ws`

程序会自动：
1. 从 API URL 提取主机和端口
2. 将 `https://` 转换为 `wss://`（或 `http://` 转换为 `ws://`）
3. 添加 `/ws` 路径

## 注意事项

- 推荐直接用 `build-widget.sh` 打包；它会把工具链缓存到本地，避免每次重新推导编译命令
- Makefile 优先使用 PATH 中的 `x86_64-w64-mingw32-gcc-posix` 或
  `x86_64-w64-mingw32-gcc`；也可以通过 `CC=/path/to/compiler` 显式指定
- `windres` 是可选的；没有它时只是不嵌入自定义图标，程序仍然可用
- 编译后的 `.exe` 文件不会提交到 git（在 .gitignore 中）

## 故障排查

如果编译失败：

1. **缺少 windres**: 确保安装了完整的 mingw-w64 包
2. **缺少头文件**: 检查是否安装了 mingw-w64-tools
3. **链接错误**: 确保所有 `-l` 参数的库都可用

## 已完成的更改

- ✅ 移除了独立 WebSocket 端口
- ✅ 改为从 API URL 动态提取主机和端口
- ✅ 添加 `/ws` 路径到 WebSocket URL
- ✅ 保持与 HTTP API 相同的主机和端口
