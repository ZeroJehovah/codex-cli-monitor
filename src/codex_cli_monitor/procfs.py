from __future__ import annotations

import os
import socket
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from .models import NetworkConnection, ProcessInfo


TCP_STATES = {
    "01": "ESTABLISHED",
    "02": "SYN_SENT",
    "03": "SYN_RECV",
    "04": "FIN_WAIT1",
    "05": "FIN_WAIT2",
    "06": "TIME_WAIT",
    "07": "CLOSE",
    "08": "CLOSE_WAIT",
    "09": "LAST_ACK",
    "0A": "LISTEN",
    "0B": "CLOSING",
}


def read_processes(proc_root: Path = Path("/proc")) -> dict[int, ProcessInfo]:
    uptime = _read_uptime(proc_root)
    ticks_per_second = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    processes: dict[int, ProcessInfo] = {}

    for pid_dir in _iter_pid_dirs(proc_root):
        process = _read_process(pid_dir, uptime, ticks_per_second)
        if process is not None:
            processes[process.pid] = process

    children: dict[int, list[int]] = defaultdict(list)
    for process in processes.values():
        if process.ppid in processes:
            children[process.ppid].append(process.pid)

    return {
        pid: replace(process, children=tuple(sorted(children.get(pid, ()))))
        for pid, process in processes.items()
    }


def read_network_connections(
    proc_root: Path = Path("/proc"), pids: Iterable[int] | None = None
) -> dict[int, tuple[NetworkConnection, ...]]:
    pid_set = set(pids) if pids is not None else None
    inode_owners = _read_socket_inode_owners(proc_root, pid_set)
    if not inode_owners:
        return {}

    result: dict[int, list[NetworkConnection]] = defaultdict(list)
    for connection in _read_tcp_table(proc_root / "net" / "tcp", "tcp4"):
        for owner in inode_owners.get(connection.inode, ()):
            result[owner].append(connection)
    for connection in _read_tcp_table(proc_root / "net" / "tcp6", "tcp6"):
        for owner in inode_owners.get(connection.inode, ()):
            result[owner].append(connection)

    return {pid: tuple(connections) for pid, connections in result.items()}


def _iter_pid_dirs(proc_root: Path) -> Iterable[Path]:
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return ()
    return (entry for entry in entries if entry.name.isdecimal() and entry.is_dir())


def _read_process(
    pid_dir: Path, uptime: float | None, ticks_per_second: int
) -> ProcessInfo | None:
    stat_text = _read_text(pid_dir / "stat")
    if stat_text is None:
        return None

    parsed = parse_stat(stat_text)
    if parsed is None:
        return None

    pid, comm, state, ppid, tty_nr, utime_ticks, stime_ticks, start_ticks = parsed
    cmdline = _read_cmdline(pid_dir / "cmdline")
    cwd = _readlink(pid_dir / "cwd")
    exe = _readlink(pid_dir / "exe")
    stdin = _readlink(pid_dir / "fd" / "0")

    elapsed_seconds = None
    if uptime is not None:
        elapsed_seconds = max(0.0, uptime - (start_ticks / ticks_per_second))

    return ProcessInfo(
        pid=pid,
        ppid=ppid,
        comm=comm,
        state=state,
        cmdline=tuple(cmdline),
        cwd=cwd,
        exe=exe,
        tty=_tty_from_fd(stdin, tty_nr),
        tty_nr=tty_nr,
        elapsed_seconds=elapsed_seconds,
        cpu_seconds=(utime_ticks + stime_ticks) / ticks_per_second,
    )


def parse_stat(
    stat_text: str,
) -> tuple[int, str, str, int, int, int, int, int] | None:
    left = stat_text.find("(")
    right = stat_text.rfind(")")
    if left < 0 or right < left:
        return None

    try:
        pid = int(stat_text[:left].strip())
        comm = stat_text[left + 1 : right]
        fields = stat_text[right + 2 :].split()
        state = fields[0]
        ppid = int(fields[1])
        tty_nr = int(fields[4])
        utime_ticks = int(fields[11])
        stime_ticks = int(fields[12])
        start_ticks = int(fields[19])
    except (IndexError, ValueError):
        return None

    return pid, comm, state, ppid, tty_nr, utime_ticks, stime_ticks, start_ticks


def _read_uptime(proc_root: Path) -> float | None:
    text = _read_text(proc_root / "uptime")
    if text is None:
        return None
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return None


def _read_cmdline(path: Path) -> list[str]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    return [
        part.decode("utf-8", errors="replace")
        for part in raw.split(b"\0")
        if part
    ]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _readlink(path: Path) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def _tty_from_fd(stdin: str | None, tty_nr: int | None) -> str | None:
    if stdin and stdin.startswith("/dev/"):
        return stdin
    if tty_nr and tty_nr > 0:
        return str(tty_nr)
    return None


def _read_socket_inode_owners(
    proc_root: Path, pids: set[int] | None
) -> dict[str, tuple[int, ...]]:
    owners: dict[str, list[int]] = defaultdict(list)
    pid_dirs = _iter_pid_dirs(proc_root)
    for pid_dir in pid_dirs:
        try:
            pid = int(pid_dir.name)
        except ValueError:
            continue
        if pids is not None and pid not in pids:
            continue

        fd_dir = pid_dir / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue

        for fd in fds:
            target = _readlink(fd)
            if not target or not target.startswith("socket:[") or not target.endswith("]"):
                continue
            inode = target.removeprefix("socket:[").removesuffix("]")
            owners[inode].append(pid)

    return {inode: tuple(sorted(pid_list)) for inode, pid_list in owners.items()}


def _read_tcp_table(path: Path, protocol: str) -> Iterable[NetworkConnection]:
    text = _read_text(path)
    if text is None:
        return ()

    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 10:
            continue
        try:
            local_address, local_port = _decode_address(parts[1], protocol)
            remote_address, remote_port = _decode_address(parts[2], protocol)
        except ValueError:
            continue
        rows.append(
            NetworkConnection(
                protocol=protocol,
                local_address=local_address,
                local_port=local_port,
                remote_address=remote_address,
                remote_port=remote_port,
                state=TCP_STATES.get(parts[3], parts[3]),
                inode=parts[9],
            )
        )
    return tuple(rows)


def _decode_address(value: str, protocol: str) -> tuple[str, int]:
    address_hex, port_hex = value.split(":", 1)
    port = int(port_hex, 16)

    if protocol == "tcp4":
        raw = bytes.fromhex(address_hex)
        address = socket.inet_ntop(socket.AF_INET, raw[::-1])
        return address, port

    raw = bytes.fromhex(address_hex)
    chunks = [raw[index : index + 4][::-1] for index in range(0, len(raw), 4)]
    address = socket.inet_ntop(socket.AF_INET6, b"".join(chunks))
    return address, port
