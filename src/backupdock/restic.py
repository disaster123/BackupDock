from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from backupdock.config import ResticConfig, RetentionConfig


class ResticError(RuntimeError):
    pass


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    unit = units[0]
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            break
        size /= 1024.0
    if unit == "B":
        return f"{int(size)} B"
    return f"{size:.1f} {unit}"


def _format_duration(seconds: int | float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class ResticRunner:
    def __init__(
        self,
        config: ResticConfig,
        *,
        dry_run: bool = False,
        env_overrides: Mapping[str, str] | None = None,
        progress: bool | None = None,
    ) -> None:
        self.config = config
        self.dry_run = dry_run
        self.env_overrides = dict(env_overrides or {})
        self.progress = (
            bool(getattr(sys.stdout, "isatty", lambda: False)())
            if progress is None
            else progress
        )

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.config.repository:
            env["RESTIC_REPOSITORY"] = self.config.repository
        if self.config.password_file:
            env["RESTIC_PASSWORD_FILE"] = str(self.config.password_file)
        env.update(self.env_overrides)
        return env

    def _effective_host(self) -> str:
        return self.config.host or self._env().get("RESTIC_HOST") or socket.gethostname()

    def _base(self) -> list[str]:
        return [self.config.binary]

    def _run(self, args: Sequence[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
        command = self._base() + list(args)
        if self.dry_run:
            print(f"DRY-RUN {shlex.join(command)}")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        try:
            result = subprocess.run(
                command,
                env=self._env(),
                text=True,
                capture_output=capture,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ResticError(f"Restic binary not found: {self.config.binary}") from exc

        if result.returncode != 0:
            detail = ""
            if capture:
                detail = (result.stderr or result.stdout or "").strip()
            suffix = f": {detail}" if detail else ""
            raise ResticError(f"Restic command failed with exit code {result.returncode}{suffix}")
        return result

    @staticmethod
    def _clear_progress(width: int) -> None:
        if width <= 0:
            return
        sys.stdout.write("\r" + (" " * width) + "\r")
        sys.stdout.flush()

    @staticmethod
    def _progress_text(message: dict, group_key: str, *, preseed: bool) -> str:
        phase = "preseed" if preseed else "backup"
        bytes_done = int(message.get("bytes_done") or 0)
        total_bytes = int(message.get("total_bytes") or 0)
        files_done = int(message.get("files_done") or 0)
        total_files = int(message.get("total_files") or 0)
        percent = float(message.get("percent_done") or 0.0)

        parts = [f"[{group_key}] {phase}"]
        if total_bytes > 0:
            parts.append(f"{percent * 100:5.1f}%")
            parts.append(f"{_format_bytes(bytes_done)} / {_format_bytes(total_bytes)}")
        else:
            parts.append(f"{_format_bytes(bytes_done)} processed")

        if total_files > 0:
            parts.append(f"{files_done:,} / {total_files:,} files")
        elif files_done > 0:
            parts.append(f"{files_done:,} files")

        remaining = int(message.get("seconds_remaining") or 0)
        if remaining > 0:
            parts.append(f"ETA {_format_duration(remaining)}")
        return "  ".join(parts)

    @staticmethod
    def _summary_text(message: dict, group_key: str, *, preseed: bool) -> str:
        phase = "preseed" if preseed else "backup"
        files = int(message.get("total_files_processed") or 0)
        processed = int(message.get("total_bytes_processed") or 0)
        stored_raw = message.get("data_added_packed")
        if stored_raw is None:
            stored_raw = message.get("data_added")
        stored = int(stored_raw or 0)
        duration = float(message.get("total_duration") or 0.0)
        snapshot_id = str(message.get("snapshot_id") or "")

        text = (
            f"[{group_key}] {phase} complete: {files:,} files, "
            f"{_format_bytes(processed)} processed, {_format_bytes(stored)} stored"
        )
        if duration > 0:
            text += f", {_format_duration(duration)}"
        if snapshot_id:
            text += f", snapshot {snapshot_id[:8]} saved"
        else:
            text += ", no new snapshot"
        return text

    def _run_backup_with_progress(
        self,
        args: Sequence[str],
        group_key: str,
        *,
        preseed: bool,
    ) -> None:
        command = self._base() + list(args)
        process: subprocess.Popen[str] | None = None
        progress_width = 0
        summary: dict | None = None
        env = self._env()
        # Restic disables periodic progress on non-interactive outputs. Its stdout is
        # intentionally piped here for JSON parsing, so explicitly request updates.
        env["RESTIC_PROGRESS_FPS"] = "2"

        try:
            process = subprocess.Popen(
                command,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
            if process.stdout is None:
                raise ResticError("Cannot read Restic progress output")

            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._clear_progress(progress_width)
                    progress_width = 0
                    print(line, flush=True)
                    continue

                message_type = message.get("message_type")
                if message_type == "status":
                    text = self._progress_text(message, group_key, preseed=preseed)
                    sys.stdout.write("\r" + text.ljust(progress_width))
                    sys.stdout.flush()
                    progress_width = max(progress_width, len(text))
                elif message_type == "summary":
                    summary = message
                elif message_type == "error":
                    self._clear_progress(progress_width)
                    progress_width = 0
                    error_value = message.get("error")
                    detail = ""
                    if isinstance(error_value, dict):
                        detail = str(error_value.get("message") or "")
                    if not detail:
                        detail = str(message.get("message") or "Restic reported an error")
                    item = str(message.get("item") or "")
                    suffix = f" ({item})" if item else ""
                    print(f"[{group_key}] restic error: {detail}{suffix}", file=sys.stderr, flush=True)

            returncode = process.wait()
        except FileNotFoundError as exc:
            raise ResticError(f"Restic binary not found: {self.config.binary}") from exc
        except BaseException:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise
        finally:
            self._clear_progress(progress_width)
            if process is not None and process.stdout is not None:
                process.stdout.close()

        if returncode != 0:
            raise ResticError(f"Restic command failed with exit code {returncode}")
        if summary is not None:
            print(self._summary_text(summary, group_key, preseed=preseed), flush=True)
        else:
            phase = "preseed" if preseed else "backup"
            print(f"[{group_key}] {phase} complete", flush=True)

    def preflight(self) -> set[str] | None:
        result = self._run(["snapshots", "--json"], capture=True)
        if self.dry_run:
            return None

        try:
            snapshots = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise ResticError("Restic snapshots returned invalid JSON") from exc
        if not isinstance(snapshots, list):
            raise ResticError("Restic snapshots returned an unexpected JSON structure")

        current_host = self._effective_host()
        consistent_groups: set[str] = set()
        for snapshot in snapshots:
            if not isinstance(snapshot, dict) or snapshot.get("hostname") != current_host:
                continue
            tags_raw = snapshot.get("tags")
            if not isinstance(tags_raw, list):
                continue
            tags = {tag for tag in tags_raw if isinstance(tag, str)}
            if "backupdock" not in tags or "backupdock-preseed" in tags:
                continue
            for tag in tags:
                prefix = "backupdock-group="
                if tag.startswith(prefix) and tag[len(prefix):]:
                    consistent_groups.add(tag[len(prefix):])
        return consistent_groups

    def init(self) -> None:
        self._run(["init"])

    def backup(self, paths: Sequence[Path], group_key: str, *, preseed: bool = False) -> None:
        if not paths:
            return
        primary_tag = "backupdock-preseed" if preseed else "backupdock"
        args = ["backup", "--tag", primary_tag, "--tag", f"backupdock-group={group_key}"]
        if self.config.host:
            args.extend(["--host", self.config.host])
        args.extend(self.config.backup_args)
        args.extend(str(path) for path in paths)

        if self.progress and not self.dry_run:
            if "--json" not in args:
                args.insert(1, "--json")
            self._run_backup_with_progress(args, group_key, preseed=preseed)
            return
        self._run(args)

    def snapshots(self) -> None:
        self._run(["snapshots", "--tag", "backupdock"])

    def check(self) -> None:
        self._run(["check"])

    def forget(self, retention: RetentionConfig, *, prune: bool | None = None) -> None:
        if not retention.configured():
            raise ResticError("No retention policy is configured")

        args = ["forget", "--tag", "backupdock"]
        for name in ("last", "daily", "weekly", "monthly", "yearly"):
            value = getattr(retention, f"keep_{name}")
            if value is not None:
                args.extend([f"--keep-{name}", str(value)])
        if retention.prune if prune is None else prune:
            args.append("--prune")
        self._run(args)
