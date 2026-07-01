# CodexMonitorWidget

Native Win32 floating status widget for `codex-cli-monitor`. The widget is
single-instance per Windows logon session; launching it again while it is
already running exits immediately without opening another floating panel.

Run the monitor API first from Linux or WSL:

```bash
PYTHONPATH=src python3 -m codex_cli_monitor --daemon
```

Build from Linux/WSL with MinGW-w64:

```bash
x86_64-w64-mingw32-gcc -Os -s -DUNICODE -D_UNICODE \
  windows/CodexMonitorWidget/src/main.c \
  -o dist/CodexMonitorWidget-win-x64/CodexMonitorWidget.exe \
  -mwindows -municode -Wl,--subsystem,windows \
  -lwinhttp -lcomctl32 -lshell32 -luser32 -lgdi32 -ladvapi32
```

The widget polls `http://localhost:8765/api/sessions` by default. To use another
endpoint, pass it as the first argument or set `CODEX_MONITOR_API_URL`.

The floating panel is a headerless table grouped by directory. Each row shows
the directory name in the first column and one or more softened-edge process
status dots in the second column. The table width is calculated from the
visible directory names and status dots instead of reserving a large fixed
directory column. Running sessions are shown with a blue breathing glow; other
status dots remain static. The widget saves its last position and display size,
restores them on launch, and keeps dynamic size changes inside the visible work
area by anchoring to the touched edge. When attached to the left or right edge,
the widget can animate into a compact dot-only view immediately after the
pointer leaves. Moving the pointer back over the widget interrupts the tuck
animation and expands the directory names again. The right-click menu has a
checked edge-tuck option; clearing it disables automatic tucking.

Right-click the floating widget to change display size, open the About page, or
choose Exit to close it.
