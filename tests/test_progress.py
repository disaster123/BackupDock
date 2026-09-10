from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from backupdock.config import ResticConfig
from backupdock.restic import ResticRunner


class _FakeProcess:
    def __init__(self) -> None:
        messages = [
            {
                "message_type": "status",
                "percent_done": 0.5,
                "total_files": 10,
                "files_done": 5,
                "total_bytes": 1024,
                "bytes_done": 512,
                "seconds_remaining": 2,
            },
            {
                "message_type": "summary",
                "total_files_processed": 10,
                "total_bytes_processed": 1024,
                "data_added_packed": 256,
                "total_duration": 3.0,
                "snapshot_id": "1234567890abcdef",
            },
        ]
        self.stdout = io.StringIO("".join(json.dumps(message) + "\n" for message in messages))

    def wait(self, timeout=None) -> int:
        del timeout
        return 0

    def poll(self) -> int:
        return 0

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


class ProgressTests(unittest.TestCase):
    def test_live_progress_forces_restic_updates_when_stdout_is_piped(self) -> None:
        runner = ResticRunner(ResticConfig(binary="restic"), progress=True)
        stdout = io.StringIO()

        with (
            patch("backupdock.restic.subprocess.Popen", return_value=_FakeProcess()) as popen,
            contextlib.redirect_stdout(stdout),
        ):
            runner.backup([Path("/data/example")], "compose:app")

        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["RESTIC_PROGRESS_FPS"], "2")
        self.assertIn("--json", popen.call_args.args[0])
        self.assertIn("[compose:app] backup", stdout.getvalue())
        self.assertIn("snapshot 12345678 saved", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
