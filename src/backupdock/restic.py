from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from backupdock.config import ResticConfig, RetentionConfig


class ResticError(RuntimeError):
    pass


class ResticRunner:
    def __init__(
        self,
        config: ResticConfig,
        *,
        dry_run: bool = False,
        env_overrides: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self.dry_run = dry_run
        self.env_overrides = dict(env_overrides or {})

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.config.repository:
            env["RESTIC_REPOSITORY"] = self.config.repository
        if self.config.password_file:
            env["RESTIC_PASSWORD_FILE"] = str(self.config.password_file)
        env.update(self.env_overrides)
        return env

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

    def preflight(self) -> None:
        self._run(["snapshots", "--json"], capture=True)

    def init(self) -> None:
        self._run(["init"])

    def backup(self, paths: Sequence[Path], group_key: str) -> None:
        if not paths:
            return
        args = ["backup", "--tag", "backupdock", "--tag", f"backupdock-group={group_key}"]
        if self.config.host:
            args.extend(["--host", self.config.host])
        args.extend(self.config.backup_args)
        args.extend(str(path) for path in paths)
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
