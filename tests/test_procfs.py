from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_cli_monitor.procfs import parse_stat, read_network_connections, read_processes


class ProcfsTests(unittest.TestCase):
    def test_parse_stat_handles_spaces_in_comm(self) -> None:
        stat = _stat_line(42, "codex worker", "S", 1, start_ticks=100)

        parsed = parse_stat(stat)

        self.assertIsNotNone(parsed)
        pid, comm, state, ppid, tty_nr, utime, stime, start = parsed
        self.assertEqual(pid, 42)
        self.assertEqual(comm, "codex worker")
        self.assertEqual(state, "S")
        self.assertEqual(ppid, 1)
        self.assertEqual(tty_nr, 34816)
        self.assertEqual(utime, 5)
        self.assertEqual(stime, 7)
        self.assertEqual(start, 100)

    def test_read_processes_builds_child_relationships(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / "uptime").write_text("200.00 0.00\n", encoding="utf-8")
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _write_process(proc, 101, "bash", "S", 100, ["bash", "-lc", "pytest"], "/work/a")

            processes = read_processes(proc)

        self.assertEqual(processes[100].children, (101,))
        self.assertEqual(processes[101].ppid, 100)
        self.assertEqual(processes[100].cwd, "/work/a")
        self.assertEqual(processes[100].tty, "/dev/pts/3")

    def test_read_network_connections_maps_socket_inode_to_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            fd_dir = proc / "100" / "fd"
            (fd_dir / "9").symlink_to("socket:[12345]")
            net_dir = proc / "net"
            net_dir.mkdir()
            (net_dir / "tcp").write_text(
                "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
                "   0: 0100007F:C350 5DB8D822:01BB 01 00000000:00000000 00:00000000 00000000  1000        0 12345 1 0000000000000000 20 4 30 10 -1\n",
                encoding="utf-8",
            )
            (net_dir / "tcp6").write_text(
                "  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n",
                encoding="utf-8",
            )

            connections = read_network_connections(proc, {100})

        self.assertEqual(len(connections[100]), 1)
        self.assertEqual(connections[100][0].remote_address, "34.216.184.93")
        self.assertEqual(connections[100][0].remote_port, 443)


def _write_process(
    proc: Path,
    pid: int,
    comm: str,
    state: str,
    ppid: int,
    cmdline: list[str],
    cwd: str,
) -> None:
    pid_dir = proc / str(pid)
    (pid_dir / "fd").mkdir(parents=True)
    (pid_dir / "stat").write_text(_stat_line(pid, comm, state, ppid), encoding="utf-8")
    (pid_dir / "cmdline").write_bytes(b"\0".join(item.encode() for item in cmdline) + b"\0")
    (pid_dir / "cwd").symlink_to(cwd)
    (pid_dir / "exe").symlink_to(f"/usr/bin/{cmdline[0]}")
    (pid_dir / "fd" / "0").symlink_to("/dev/pts/3")


def _stat_line(
    pid: int,
    comm: str,
    state: str,
    ppid: int,
    tty_nr: int = 34816,
    utime: int = 5,
    stime: int = 7,
    start_ticks: int = 100,
) -> str:
    fields = [
        state,
        str(ppid),
        "0",
        "0",
        str(tty_nr),
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        str(utime),
        str(stime),
        "0",
        "0",
        "20",
        "0",
        "1",
        "0",
        str(start_ticks),
    ]
    return f"{pid} ({comm}) {' '.join(fields)}\n"


if __name__ == "__main__":
    unittest.main()
