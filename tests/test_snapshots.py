from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backupdock.cli import main
from backupdock.config import AppConfig, ResticConfig
from backupdock.restic import ResticError, ResticRunner
from backupdock.snapshots import select_snapshots


NOW = datetime(2026, 1, 20, 8, tzinfo=timezone.utc)


@contextlib.contextmanager
def local_timezone(name):
    try:
        with patch.dict(os.environ, {"TZ": name}):
            time.tzset()
            yield
    finally:
        time.tzset()


def snapshot(char, *, group="compose:app", host="source.example", time="2026-01-20T03:00:00Z", tags=None):
    return {
        "id": char * 64, "time": time, "hostname": host,
        "tags": tags if tags is not None else ["backupdock", f"backupdock-group={group}"],
        "paths": ["/example/data", "/example/manifest.json"],
        "summary": {"total_bytes_processed": 1024},
    }


class SnapshotSelectionTests(unittest.TestCase):
    def select(self, values, **kwargs):
        return select_snapshots(values, all_snapshots=kwargs.get("all_snapshots", False), today=kwargs.get("today", False), now=kwargs.get("now", NOW))

    def test_latest_is_per_host_and_group_and_keeps_stale_groups_visible(self):
        values = [snapshot("a", time="2026-01-19T03:00:00Z"), snapshot("b"),
                  snapshot("c", group="compose:db", time="2026-01-19T04:00:00Z"),
                  snapshot("d", host="other.example")]
        records, current, groups = self.select(values)
        self.assertEqual({r[0]["id"] for r in records}, {char * 64 for char in "bcd"})
        self.assertEqual((current, groups), (2, 3))

    def test_history_and_today_filter_preserve_all_matching_snapshots(self):
        values = [snapshot("a", time="2026-01-19T03:00:00Z"), snapshot("b"),
                  snapshot("c", time="2026-01-20T04:00:00Z")]
        records, current, groups = self.select(values, all_snapshots=True, today=True)
        self.assertEqual([r[0]["id"] for r in records], ["c" * 64, "b" * 64])
        self.assertEqual((current, groups), (1, 1))

    def test_today_uses_local_calendar_date_instead_of_utc_date(self):
        now = NOW.replace(tzinfo=timezone(timedelta(hours=2)))
        values = [snapshot("a", time="2026-01-19T23:30:00Z"), snapshot("b", group="compose:db", time="2026-01-19T21:30:00Z")]
        with local_timezone("Etc/GMT-2"):
            records, current, groups = self.select(values, today=True, now=now)
        self.assertEqual([r[0]["id"] for r in records], ["a" * 64])
        self.assertEqual((current, groups), (1, 2))

    def test_today_respects_offset_at_snapshot_time_across_dst_change(self):
        now = datetime(2026, 3, 29, 8, tzinfo=timezone(timedelta(hours=2)))
        with local_timezone("Europe/Berlin"):
            records, current, groups = self.select([snapshot("a", time="2026-03-28T22:30:00Z")], today=True, now=now)
        self.assertEqual((records, current, groups), ([], 0, 1))

    def test_latest_compares_instants_with_different_offsets(self):
        values = [snapshot("a", time="2026-01-20T04:00:00+02:00"), snapshot("b", time="2026-01-20T03:00:00Z")]
        self.assertEqual(self.select(values)[0][0][0]["id"], "b" * 64)

    def test_preseeds_and_unrelated_snapshots_do_not_count_as_normal(self):
        values = [snapshot("a", tags=["backupdock-preseed", "backupdock-group=compose:app"]),
                  snapshot("b", tags=["backupdock", "backupdock-preseed"]), snapshot("c", tags=["other"])]
        self.assertEqual(self.select(values), ([], 0, 0))

    def test_missing_group_tags_do_not_collapse_unrelated_snapshots(self):
        records, _, groups = self.select([snapshot("a", tags=["backupdock"]), snapshot("b", tags=["backupdock"])])
        self.assertEqual((len(records), groups), (2, 2))

    def test_invalid_metadata_is_reported_instead_of_showing_false_success(self):
        for value in [None, snapshot("a", time="invalid"), snapshot("a", time="2026-01-20T03:00:00"), snapshot("a", tags="backupdock")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.select([value])


class SnapshotOutputTests(unittest.TestCase):
    def run_listing(self, values, **kwargs):
        runner = ResticRunner(ResticConfig())
        output = io.StringIO()
        def response(args, **options):
            if args[0] == "snapshots":
                payload = values
            elif args[0] == "stats":
                payload = {"total_size": 512}
            else:
                raise AssertionError(f"Unexpected Restic command: {args}")
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload))

        with patch.object(runner, "_run", side_effect=response) as run, patch("backupdock.restic.datetime") as clock, contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            clock.now.return_value = NOW
            runner.snapshots(**kwargs)
        return output.getvalue(), run

    def test_default_table_omits_paths_and_raw_tags_and_reports_today_coverage(self):
        output, run = self.run_listing([snapshot("a", time="2026-01-19T03:00:00Z"), snapshot("b")])
        self.assertIn("bbbbbbbb", output)
        self.assertNotIn("aaaaaaaa", output)
        self.assertIn("compose:app", output)
        self.assertIn("1.0 KiB", output)
        self.assertIn("1/1", output)
        self.assertIn("consult the run log", output)
        self.assertNotIn("/example/data", output)
        self.assertNotIn("backupdock-group=", output)
        self.assertEqual(run.call_args_list[0].args[0], ["snapshots", "--tag", "backupdock", "--json"])
        self.assertEqual(run.call_args_list[1].args[0], ["stats", "--mode", "raw-data", "--json", "a" * 64, "b" * 64])
        self.assertIn("GROUP STORED", output)
        self.assertIn("512 B", output)

    def test_missing_size_statistics_are_unknown_and_zero_is_valid(self):
        values = [snapshot("a"), snapshot("b", group="compose:db")]
        values[0].pop("summary")
        values[1]["summary"]["total_bytes_processed"] = 0
        output, _ = self.run_listing(values)
        self.assertIn("unknown", output)
        self.assertIn("0 B", output)

    def test_no_today_snapshot_keeps_known_group_count(self):
        output, _ = self.run_listing([snapshot("a", time="2026-01-19T03:00:00Z")], today=True)
        self.assertIn("No matching", output)
        self.assertIn("0/1", output)

    def test_json_returns_full_raw_metadata_without_human_output(self):
        values = [snapshot("a"), snapshot("b")]
        output, _ = self.run_listing(values, json_output=True)
        self.assertEqual(json.loads(output), values)

    def test_details_retains_original_restic_command(self):
        runner = ResticRunner(ResticConfig())
        with patch.object(runner, "_run") as run:
            runner.snapshots(details=True)
        run.assert_called_once_with(["snapshots", "--tag", "backupdock"])

    def test_invalid_json_and_structure_raise_recoverable_error(self):
        runner = ResticRunner(ResticConfig())
        for text in ["not JSON", "{}", "[null]"]:
            with self.subTest(text=text), patch.object(runner, "_run", return_value=subprocess.CompletedProcess([], 0, stdout=text)), self.assertRaises(ResticError):
                runner.snapshots()

    def test_cli_forwards_compact_filters(self):
        with patch("backupdock.cli.load_config", return_value=AppConfig()), patch("backupdock.cli.ResticRunner") as runner:
            self.assertEqual(main(["snapshots", "--all", "--today"]), 0)
            runner.return_value.snapshots.assert_called_once_with(all_snapshots=True, today=True, details=False, json_output=False, manifest_dir=Path("/var/lib/backupdock/manifests"))

    def test_cli_rejects_filters_with_full_listing_formats(self):
        for option in ["--details", "--json"]:
            with self.subTest(option=option), patch("backupdock.cli.load_config", return_value=AppConfig()), patch("backupdock.cli.ResticRunner") as runner, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["snapshots", option, "--today"]), 1)
                runner.return_value.snapshots.assert_not_called()

    def test_today_counts_and_sizes_include_history_and_keep_hosts_separate(self):
        values = [snapshot("a", time="2026-01-19T03:00:00Z"), snapshot("b"), snapshot("c", host="other.example")]
        output, run = self.run_listing(values, today=True)
        commands = [call.args[0] for call in run.call_args_list if call.args[0][0] == "stats"]
        self.assertEqual(len(commands), 2)
        self.assertEqual({tuple(command[4:]) for command in commands}, {("a" * 64, "b" * 64), ("c" * 64,)})
        self.assertNotIn("aaaaaaaa", output)
        self.assertIn("source.example", output)

    def test_all_history_fetches_group_statistics_once(self):
        _, run = self.run_listing([snapshot("a"), snapshot("b")], all_snapshots=True)
        self.assertEqual(sum(call.args[0][0] == "stats" for call in run.call_args_list), 1)


class SnapshotEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.runner = ResticRunner(ResticConfig())
        self.manifest_dir = Path("/example/state/manifests")
        self.value = snapshot("a")
        self.path = "/old/state/manifests/compose_app.json"
        self.value["paths"].append(self.path)

    def manifest(self, *, group="compose:app", containers=None):
        return {"schema": 1, "group": {"key": group}, "containers": containers if containers is not None else [{"id": "id-db"}, {"id": "id-web"}]}

    def count(self, payload):
        with patch.object(self.runner, "_run", return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(payload))) as run:
            count = self.runner._snapshot_container_count(self.value, "compose:app", self.manifest_dir)
        return count, run

    def test_container_count_reads_archived_manifest_at_original_path(self):
        count, run = self.count(self.manifest())
        self.assertEqual(count, 2)
        run.assert_called_once_with(["dump", "a" * 64, self.path], capture=True)

    def test_manifest_under_parent_backup_path_uses_configured_state_directory(self):
        self.value["paths"] = ["/example/state"]
        count, run = self.count(self.manifest(containers=[]))
        self.assertEqual(count, 0)
        run.assert_called_once_with(["dump", "a" * 64, str(self.manifest_dir / "compose_app.json")], capture=True)

    def test_unavailable_or_invalid_container_metadata_is_unknown(self):
        payloads = [None, {"schema": 2}, self.manifest(group="compose:other"),
                    self.manifest(containers=[None]), self.manifest(containers=[{"id": "same"}, {"id": "same"}])]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertIsNone(self.count(payload)[0])
        with patch.object(self.runner, "_run", side_effect=ResticError("file unavailable")):
            self.assertIsNone(self.runner._snapshot_container_count(self.value, "compose:app", self.manifest_dir))

    def test_absent_manifest_and_host_group_do_not_issue_dump(self):
        with patch.object(self.runner, "_run") as run:
            self.assertIsNone(self.runner._snapshot_container_count(snapshot("a"), "compose:app", self.manifest_dir))
            self.assertEqual(self.runner._snapshot_container_count(snapshot("b", group="host"), "host", self.manifest_dir), 0)
        run.assert_not_called()

    def test_group_size_rejects_invalid_values_instead_of_reporting_zero(self):
        for payload in [None, {}, {"total_size": -1}, {"total_size": True}, {"total_size": "1024"}]:
            with self.subTest(payload=payload), patch.object(self.runner, "_run", return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(payload))), self.assertRaises(ResticError):
                self.runner._snapshot_group_size(["a" * 64])

    def test_container_counts_are_per_displayed_snapshot_in_history(self):
        values = [self.value, {**self.value, "id": "b" * 64}]
        output = io.StringIO()
        def response(args, **kwargs):
            if args[0] == "snapshots":
                payload = values
            elif args[0] == "stats":
                payload = {"total_size": 700}
            else:
                payload = self.manifest(containers=[{"id": "id-db"}] if args[1] == "a" * 64 else [{"id": "id-db"}, {"id": "id-web"}])
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload))
        with patch.object(self.runner, "_run", side_effect=response), contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            self.runner.snapshots(all_snapshots=True, manifest_dir=self.manifest_dir)
        rows = {line.split()[-1]: line.split() for line in output.getvalue().splitlines() if line.startswith("compose:app")}
        self.assertEqual(rows["aaaaaaaa"][3], "1")
        self.assertEqual(rows["bbbbbbbb"][3], "2")


if __name__ == "__main__":
    unittest.main()
