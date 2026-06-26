from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from codex_cli_monitor.shim import find_real_codex, write_launch_record_best_effort


class ShimTests(unittest.TestCase):
    def test_find_real_codex_skips_the_shim_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shim_dir = root / "shim"
            real_dir = root / "real"
            shim_dir.mkdir()
            real_dir.mkdir()
            shim = shim_dir / "codex"
            real = real_dir / "codex"
            shim.write_text("#!/bin/sh\n", encoding="utf-8")
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            shim.chmod(0o755)
            real.chmod(0o755)

            found = find_real_codex(
                "codex",
                env={"PATH": os.pathsep.join([str(shim_dir), str(real_dir)])},
                shim_path=shim,
            )

        self.assertEqual(found, real)

    def test_launch_record_write_is_best_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "as-directory"
            directory.mkdir()

            with redirect_stderr(StringIO()):
                result = write_launch_record_best_effort(
                    directory,
                    ("codex",),
                    Path("/usr/bin/codex"),
                )

        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
