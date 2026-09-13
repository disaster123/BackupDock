from __future__ import annotations

import contextlib
import io
import json
import subprocess
import unittest
from copy import deepcopy
from unittest.mock import patch

from backupdock.config import ResticConfig, RetentionConfig
from backupdock.restic import ResticError, ResticRunner


def snapshot(number, *, preseed=False, time="2026-09-13T12:00:00Z", host="source", group="compose:app", paths=None):
    return {
        "id": f"{number:064x}", "time": time, "hostname": host,
        "paths": ["/data", "/state/manifests/app.json"] if paths is None else paths,
        "tags": ["backupdock-preseed" if preseed else "backupdock", f"backupdock-group={group}"],
    }


class PreseedRetentionTests(unittest.TestCase):
    def _forget(self, remaining, *, retention=None, prune=None):
        runner = ResticRunner(ResticConfig(), progress=False)
        commands = []

        def run(args, *, capture=False):
            commands.append(list(args))
            output = json.dumps(remaining) if args == ["snapshots", "--json"] else ""
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

        with patch.object(runner, "_run", side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            runner.forget(retention or RetentionConfig(keep_last=1), prune=prune)
        return commands

    def test_retention_and_preseed_cleanup_are_separate_and_prune_runs_last(self):
        preseed = snapshot(1, preseed=True)
        normal = snapshot(2, time="2026-09-13T12:01:00Z")
        commands = self._forget([preseed, normal], retention=RetentionConfig(keep_daily=14, keep_weekly=8, keep_monthly=12), prune=True)
        self.assertEqual(commands, [
            ["forget", "--tag", "backupdock", "--keep-daily", "14", "--keep-weekly", "8", "--keep-monthly", "12"],
            ["snapshots", "--json"], ["forget", preseed["id"]], ["prune"],
        ])

    def test_no_remaining_consistent_snapshot_preserves_preseed(self):
        self.assertEqual(self._forget([snapshot(1, preseed=True)]), [
            ["forget", "--tag", "backupdock", "--keep-last", "1"], ["snapshots", "--json"],
        ])

    def test_other_hosts_groups_paths_and_older_normals_do_not_replace_preseed(self):
        variants = [
            snapshot(2, host="another-source"), snapshot(2, group="compose:other"),
            snapshot(2, paths=["/data"]), snapshot(2, time="2026-09-13T11:00:00Z"),
        ]
        for normal in variants:
            with self.subTest(normal=normal):
                self.assertEqual(len(self._forget([snapshot(1, preseed=True), normal])), 2)

    def test_equal_timestamp_and_reordered_paths_allow_replacement(self):
        preseed = snapshot(1, preseed=True)
        normal = snapshot(2, paths=list(reversed(preseed["paths"])))
        self.assertEqual(self._forget([preseed, normal])[-1], ["forget", preseed["id"]])

    def test_nanoseconds_and_timezone_offsets_are_compared_correctly(self):
        preseed = snapshot(1, preseed=True, time="2026-09-13T12:00:00.123456799Z")
        for normal_time, removable in (
            ("2026-09-13T12:00:00.123456700Z", False),
            ("2026-09-13T14:00:00.123456800+02:00", True),
        ):
            with self.subTest(time=normal_time):
                commands = self._forget([preseed, snapshot(2, time=normal_time)])
                self.assertEqual(len(commands), 3 if removable else 2)

    def test_unrelated_snapshots_and_malformed_metadata_are_preserved(self):
        mutations = [
            {"id": "--prune"}, {"id": None}, {"hostname": ""}, {"paths": []},
            {"paths": [None]}, {"time": "invalid"}, {"time": "2026-09-13T12:00:00"},
            {"time": "2026-09-13T12:00:00.1234567891Z"},
            {"tags": ["backupdock-preseed"]}, {"tags": [None]},
            {"tags": ["backupdock-preseed", "backupdock-group="]},
            {"tags": ["backupdock", "backupdock-preseed", "backupdock-group=compose:app"]},
            {"tags": ["backupdock-preseed", "backupdock-group=compose:app", "backupdock-group=compose:other"]},
            {"tags": ["unrelated", "backupdock-group=compose:app"]},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                preseed = snapshot(1, preseed=True)
                preseed.update(mutation)
                self.assertEqual(len(self._forget([preseed, snapshot(2)])), 2)
        self.assertEqual(len(self._forget([None, {}, "invalid", snapshot(2)])), 2)

    def test_incomplete_normal_snapshot_cannot_authorize_removal(self):
        normal = snapshot(2)
        del normal["time"]
        self.assertEqual(len(self._forget([snapshot(1, preseed=True), normal])), 2)

    def test_prune_configuration_and_explicit_override(self):
        for configured, override, expected in ((False, None, False), (True, None, True), (True, False, False), (False, True, True)):
            with self.subTest(configured=configured, override=override):
                commands = self._forget([], retention=RetentionConfig(keep_last=1, prune=configured), prune=override)
                self.assertEqual(["prune"] in commands, expected)
                self.assertTrue(all("--prune" not in command for command in commands))

    def test_large_cleanup_is_batched_and_ids_are_deduplicated(self):
        preseeds = [snapshot(i, preseed=True) for i in range(1, 301)]
        commands = self._forget(preseeds + [deepcopy(preseeds[0]), snapshot(301)], prune=True)
        deletions = commands[2:-1]
        self.assertEqual([len(command) - 1 for command in deletions], [256, 44])
        self.assertEqual({value for command in deletions for value in command[1:]}, {s["id"] for s in preseeds})

    def test_snapshot_query_or_cleanup_failure_prevents_pruning(self):
        for failure_command in ("snapshots", "forget"):
            with self.subTest(command=failure_command):
                runner = ResticRunner(ResticConfig(), progress=False)
                commands = []

                def run(args, **kwargs):
                    commands.append(list(args))
                    if args[0] == failure_command and (args[0] != "forget" or "--tag" not in args):
                        raise ResticError("maintenance failed")
                    return subprocess.CompletedProcess(args, 0, stdout=json.dumps([snapshot(1, preseed=True), snapshot(2)]))

                with patch.object(runner, "_run", side_effect=run), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ResticError):
                    runner.forget(RetentionConfig(keep_last=1), prune=True)
                self.assertNotIn(["prune"], commands)

    def test_invalid_snapshot_response_aborts_cleanup_and_pruning(self):
        for output in ("", "invalid", "{}", "null"):
            with self.subTest(output=output):
                runner = ResticRunner(ResticConfig(), progress=False)
                with patch.object(runner, "_run", return_value=subprocess.CompletedProcess([], 0, stdout=output)) as run:
                    with self.assertRaises(ResticError):
                        runner.forget(RetentionConfig(keep_last=1), prune=True)
                self.assertEqual(run.call_count, 2)

    def test_missing_retention_policy_does_not_mutate_repository(self):
        runner = ResticRunner(ResticConfig(), progress=False)
        with patch.object(runner, "_run") as run, self.assertRaises(ResticError):
            runner.forget(RetentionConfig())
        run.assert_not_called()

    def test_normal_retention_failure_prevents_preseed_cleanup(self):
        runner = ResticRunner(ResticConfig(), progress=False)
        with patch.object(runner, "_run", side_effect=ResticError("retention failed")) as run:
            with self.assertRaises(ResticError):
                runner.forget(RetentionConfig(keep_last=1), prune=True)
        run.assert_called_once_with(["forget", "--tag", "backupdock", "--keep-last", "1"])

    def test_dry_run_does_not_invent_preseed_snapshot_ids(self):
        runner = ResticRunner(ResticConfig(), dry_run=True, progress=False)
        stdout = io.StringIO()
        with patch("backupdock.restic.run_process") as process, contextlib.redirect_stdout(stdout):
            runner.forget(RetentionConfig(keep_last=1), prune=True)
        process.assert_not_called()
        self.assertIn("DRY-RUN preseed cleanup decision unavailable", stdout.getvalue())
        self.assertIn("DRY-RUN restic prune", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
