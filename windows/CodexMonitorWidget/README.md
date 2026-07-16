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
```

The widget polls `http://localhost:8765/api/sessions` by default. To use another
endpoint, pass it as the first argument or set `CODEX_MONITOR_API_URL`.
When the API requires a Bearer token, set `CODEX_MONITOR_API_TOKEN`; the token is
sent in the `Authorization` header and is not accepted as a command-line
argument.

For persistent desktop startup, copy `start-widget.ps1.example` to
`start-widget.ps1`, fill in the aggregator URL and read token, and run it once.
It registers a scheduled task for the current user's logon. A Windows service
is intentionally not used because interactive GUI applications cannot display
from the service session.

The floating panel is a headerless table grouped by directory. Each row shows
the directory name in the first column and one or more softened-edge process
status dots in the second column. A colored vertical bar at the far left of
each row identifies the server without prefixing the directory label. Colors
are randomly selected from a high-contrast gold, purple, magenta, and neutral
preset palette that avoids the blue, green, and red process-status colors and
nearby hues. They remain stable while the server has any visible sessions and
are released after all sessions from that server disappear. The table width is
calculated from the
visible directory names and status dots instead of reserving a large fixed
directory column. Running sessions are shown with a blue breathing glow,
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
right-click menu has a
checked edge-tuck option; clearing it disables automatic tucking.

For an aggregated multi-server response, rows are grouped by both server id and
directory. Server color bars distinguish the rows, while hover details include
the server name. Rows are sorted by server name first, with server id as a
stable tie-breaker, and then by the earliest process start time within each
server. Status dots keep their process start-time ordering.

Right-click the floating widget to change display size, open the About page, or
choose Exit to close it.
