from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import time
import unittest
from datetime import datetime, timedelta, timezone
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
        with patch.object(runner, "_run", return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(values))) as run, patch("backupdock.restic.datetime") as clock, contextlib.redirect_stdout(output):
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
        run.assert_called_once_with(["snapshots", "--tag", "backupdock", "--json"], capture=True)

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
            runner.return_value.snapshots.assert_called_once_with(all_snapshots=True, today=True, details=False, json_output=False)

    def test_cli_rejects_filters_with_full_listing_formats(self):
        for option in ["--details", "--json"]:
            with self.subTest(option=option), patch("backupdock.cli.load_config", return_value=AppConfig()), patch("backupdock.cli.ResticRunner") as runner, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["snapshots", option, "--today"]), 1)
                runner.return_value.snapshots.assert_not_called()


if __name__ == "__main__":
    unittest.main()
