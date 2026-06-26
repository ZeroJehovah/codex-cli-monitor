# CodexMonitorWidget

Windows floating status widget for `codex-cli-monitor`.

Run the monitor API first from Linux or WSL:

```bash
PYTHONPATH=src python3 -m codex_cli_monitor --daemon
```

Build and run on Windows:

```powershell
dotnet run --project .\windows\CodexMonitorWidget\CodexMonitorWidget.csproj
```

The widget polls `http://localhost:8765/api/sessions` by default. To use another
endpoint, pass it as the first argument or set `CODEX_MONITOR_API_URL`.
