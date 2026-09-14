from __future__ import annotations

from datetime import datetime


def select_snapshots(snapshots: list, *, all_snapshots: bool, today: bool, now: datetime):
    records = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            raise ValueError("Restic snapshots returned invalid snapshot metadata")
        tags = snapshot.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError("Restic snapshots returned invalid snapshot tags")
        if "backupdock" not in tags or "backupdock-preseed" in tags:
            continue
        try:
            timestamp = datetime.fromisoformat(snapshot["time"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Restic snapshots returned an invalid timestamp") from exc
        if timestamp.tzinfo is None:
            raise ValueError("Restic snapshots returned a timestamp without a timezone")
        snapshot_id = snapshot.get("id")
        hostname = snapshot.get("hostname")
        if not isinstance(snapshot_id, str) or not snapshot_id or not isinstance(hostname, str):
            raise ValueError("Restic snapshots returned invalid snapshot identity")
        groups = {tag.split("=", 1)[1] for tag in tags if tag.startswith("backupdock-group=")}
        group = next(iter(groups)) if len(groups) == 1 and "" not in groups else f"unknown:{snapshot_id[:8]}"
        records.append((snapshot, timestamp, hostname, group))

    latest = {}
    for record in records:
        key = record[2:]
        if key not in latest or (record[1], record[0]["id"]) > (latest[key][1], latest[key][0]["id"]):
            latest[key] = record
    current = sum(record[1].astimezone().date() == now.date() for record in latest.values())
    selected = records if all_snapshots else list(latest.values())
    if today:
        selected = [record for record in selected if record[1].astimezone().date() == now.date()]
    selected.sort(key=lambda record: (record[2], record[3], -record[1].timestamp(), record[0]["id"]))
    return selected, current, len(latest)


def render_snapshots(records, *, current: int, groups: int, now: datetime, format_bytes) -> None:
    if records:
        rows = [("GROUP", "HOST", "SNAPSHOT TIME", "SIZE", "ID")]
        for snapshot, timestamp, hostname, group in records:
            summary = snapshot.get("summary")
            size = summary.get("total_bytes_processed") if isinstance(summary, dict) else None
            size_text = format_bytes(size) if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else "unknown"
            rows.append((group, hostname, timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S"), size_text, snapshot["id"][:8]))
        widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
        for row in rows:
            print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())
    else:
        print("No matching normal BackupDock snapshots.")
    print(f"\n{len(records)} snapshot(s) shown; {current}/{groups} known host/group(s) have their latest snapshot dated today ({now.date()}, local time).")
    print("Snapshot times do not confirm whole-run completion, container recovery, or repository checks; consult the run log.")
