# Windows 悬浮窗编译说明

## 更新内容

✅ **已更新**: WebSocket 端点现在与 HTTP API 共用端口，并使用 `/ws` 路径
- 现在 WebSocket 与 HTTP API 使用同一端口
- 自动从 API URL 提取主机和端口信息
- 添加 `/ws` 路径到 WebSocket 连接

## 编译环境要求

需要在 Linux/WSL 环境中使用 MinGW-w64 交叉编译工具：

```bash
sudo apt-get update
sudo apt-get install -y mingw-w64
```

## 编译步骤

在项目根目录执行：

```bash
cd /opt/dev/projects/personal/codex-cli-monitor

# 清理旧的构建文件
rm -rf dist/CodexMonitorWidget-win-x64
mkdir -p dist/CodexMonitorWidget-win-x64

# 编译 C 源代码
x86_64-w64-mingw32-gcc -Os -s -DUNICODE -D_UNICODE \
  windows/CodexMonitorWidget/src/main.c \
  windows/CodexMonitorWidget/src/websocket.c \
  -o dist/CodexMonitorWidget-win-x64/CodexMonitorWidget.exe \
  -mwindows -municode -Wl,--subsystem,windows \
  -lwinhttp -lcomctl32 -lshell32 -luser32 -lgdi32 -ladvapi32 -lwinmm -lmsimg32 -lws2_32

# 复制配置文件
cp windows/CodexMonitorWidget/CodexMonitorWidget.ini.example \
  dist/CodexMonitorWidget-win-x64/CodexMonitorWidget.ini

echo "✅ 编译完成！输出目录: dist/CodexMonitorWidget-win-x64/"
```

如果交叉编译器与目标汇编器/链接器位于不同目录，可以把 `binutils-mingw-w64-x86-64`
解压后的 `usr/bin` 目录通过 `BINUTILS_DIR` 传给 Makefile：

```bash
make -C windows/CodexMonitorWidget \
  BINUTILS_DIR=/tmp/codex-mingw-binutils/root/usr/bin \
  MINGW_INCLUDE_DIR=/path/to/mingw-w64/include
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
