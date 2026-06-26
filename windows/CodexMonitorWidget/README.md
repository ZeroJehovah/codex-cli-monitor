# CodexMonitorWidget

Native Win32 floating status widget for `codex-cli-monitor`.

Run the monitor API first from Linux or WSL:

```bash
PYTHONPATH=src python3 -m codex_cli_monitor --daemon
```

Build from Linux/WSL with MinGW-w64:

```bash
x86_64-w64-mingw32-gcc -Os -s -DUNICODE -D_UNICODE \
  windows/CodexMonitorWidget/src/main.c \
  -o dist/CodexMonitorWidget-win-x64/CodexMonitorWidget.exe \
  -mwindows -municode \
  -lwinhttp -lcomctl32 -lshell32 -luser32 -lgdi32
```

The widget polls `http://localhost:8765/api/sessions` by default. To use another
endpoint, pass it as the first argument or set `CODEX_MONITOR_API_URL`.

The floating panel is a headerless table grouped by directory. Each row shows
the directory name in the first column and one or more process status dots in
the second column. The table width is calculated from the visible directory
names and status dots instead of reserving a large fixed directory column.
Running sessions are shown with a breathing blue dot; other status dots remain
static.

Right-click the floating widget and choose Exit to close it.
