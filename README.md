# codex-cli-monitor

`codex-cli-monitor` is a low-intrusion sidecar monitor for local Codex CLI
sessions on Linux and WSL-style hosts. It observes process facts from `/proc`
and reports inferred session states with confidence and evidence instead of
claiming internal Codex knowledge.

## Usage

Run the sidecar monitor from the checkout:

```bash
PYTHONPATH=src python3 -m codex_cli_monitor
```

JSON output is available for scripts:

```bash
PYTHONPATH=src python3 -m codex_cli_monitor --json
```

The default scan samples process CPU time over a short window. Use
`--sample-window 0` for a single read-only snapshot, or `--watch 2` to refresh
every two seconds.

## Optional same-name shim

The `bin/codex` shim can be placed earlier in `PATH`. It records launch metadata
to `${XDG_STATE_HOME:-~/.local/state}/codex-cli-monitor/launches.jsonl`, then
`exec`s the next real `codex` executable found later in `PATH`.

```bash
export PATH="$PWD/bin:$PATH"
codex
```

The real Codex CLI receives the original arguments and working directory
semantics. The shim does not patch Codex or alter Codex runtime internals.

## Status model

Every reported session has a confirmed `open` status because a Codex process
exists. The monitor also reports one inferred status:

- `waiting_user_likely`
- `api_inflight_likely`
- `tool_running_likely`
- `active_likely`
- `unknown`

Network connections are treated as supporting evidence only. A long-lived
keep-alive connection can look similar to an in-flight API request, so API
states remain explicitly inferred unless a future structured event source is
added.

## Development

Run tests with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```
