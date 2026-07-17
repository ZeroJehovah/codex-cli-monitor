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
cp windows/CodexMonitorWidget/CodexMonitorWidget.ini.example \
  dist/CodexMonitorWidget-win-x64/CodexMonitorWidget.ini
```

The output directory contains exactly `CodexMonitorWidget.exe` and
`CodexMonitorWidget.ini`. Edit the INI beside the executable:

```ini
[CodexMonitorWidget]
ApiUrl=https://monitor.example.com/api/sessions
ApiToken=replace-with-the-read-token
```

Double-click `CodexMonitorWidget.exe` to launch it. No PowerShell script,
environment setup, Windows service, or scheduled-task installer is required.
The INI stores the read token in plaintext and should remain private to the
Windows user. For compatibility, the first command-line argument can still
override the API URL; when the INI is absent, `CODEX_MONITOR_API_URL` and
`CODEX_MONITOR_API_TOKEN` remain available as fallbacks.

The floating panel is a headerless table grouped by directory. Each row shows
the directory name in the first column and one or more softened-edge process
status dots in the second column. A colored vertical bar at the far left of
each row identifies the server without prefixing the directory label. Colors
are randomly selected from a high-contrast gold, purple, magenta, and neutral
preset palette that avoids the blue, green, and red process-status colors and
nearby hues. Visually similar colors may remain in the palette, but neighboring
servers in the sorted display order are assigned colors above a minimum RGB
distance. Assignments remain stable across ordinary refreshes and are changed
only when necessary to repair a new low-contrast adjacency after servers are
added or removed. They are released after all sessions from that server
disappear. The table width is
calculated from the visible directory names and status dots instead of reserving
a large fixed directory column. The panel has no separate gray left border:
each server color bar is flush with the visible left edge and acts as that row's
left border, with the first and last bars meeting the top and bottom borders.
Running sessions are shown with a blue breathing glow,
successful or idle sessions are green, and failed sessions are red; non-running
status dots remain static. The widget saves its last position and display size,
restores them on launch, and keeps dynamic size changes inside the visible work
area by anchoring to the touched edge. When attached to the left or right edge,
the widget can animate into a compact view immediately after the pointer
leaves. This hides only the directory names; the server color bars remain, and
the process status dots smoothly morph into narrow vertical capsule bars to
reduce width. A running blue bar keeps the same breathing glow as the expanded
running dot. Moving the pointer back over the widget interrupts the tuck
animation and expands the directory names and circular dots again. The
fully tucked view uses one equal horizontal gap from the server bar to the first
status bar, between status bars, and from the final status bar to the right
border. The right-click menu has a
checked edge-tuck option; clearing it disables automatic tucking.

When there are no session rows, the widget uses a dedicated single-row empty
state instead of a border-only rectangle. It shows `正在连接` before the first
backend response, `暂无会话` after a successful empty response, or `连接失败`
when no session data is available after a request error. A neutral accent bar
fills the visible left edge and a softened neutral indicator avoids the blue,
green, and red session-status colors. The empty state also edge-tucks: its text
fades out and the indicator morphs into a narrow capsule while the accent bar
remains visible.

For an aggregated multi-server response, rows are grouped by both server id and
directory. Server color bars distinguish the rows, while hover details include
the server name. Rows are sorted by server name first, with server id as a
stable tie-breaker, and then by the earliest process start time within each
server. Status dots keep their process start-time ordering.

Right-click the floating widget to change display size, open the About page, or
choose Exit to close it.
