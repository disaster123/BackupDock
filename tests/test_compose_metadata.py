from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backupdock.backup import BackupOrchestrator
from backupdock.config import AppConfig, BackupConfig, ProjectConfig
from backupdock.discovery import DiscoveryError, discover_groups
from backupdock.restore import TestRestore
from test_backup import FakeDocker, FakeRestic
from test_discovery import container


class ComposeEnvironmentDiscoveryTests(unittest.TestCase):
    def group(self, root, env_file, *, config=None):
        compose = root / "compose.yaml"
        compose.write_text("services:\n  web:\n    image: example/app:1.0\n    env_file: " + env_file + "\n")
        instance = container("web", project="app", config_files=(str(compose),), working_dir=str(root))
        return discover_groups([instance], config or AppConfig())[0]

    def test_relative_service_env_file_is_required_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.env"
            settings.write_text("TOKEN=value\n")
            group = self.group(root, "settings.env")
            source = next(source for source in group.sources if source.path == settings)
            self.assertEqual(source.kind, "compose")
            self.assertTrue(source.required)

    def test_list_absolute_paths_parent_paths_and_raw_format_are_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            working = root / "project"
            working.mkdir()
            first = working / "settings.env"
            second = root / "shared.env"
            first.write_text("TOKEN=value\n")
            second.write_text("OTHER=value\n")
            group = self.group(working, f"\n      - settings.env\n      - ../shared.env\n      - path: '{second}'\n        format: raw")
            paths = [source.path for source in group.sources]
            self.assertIn(first, paths)
            self.assertEqual(paths.count(second), 1)

    def test_required_missing_file_fails_discovery_before_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(DiscoveryError, "Required Compose environment file"):
                self.group(Path(directory), "settings.env")

    def test_optional_missing_file_does_not_fail_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            group = self.group(root, "\n      - path: optional.env\n        required: false")
            source = next(source for source in group.sources if source.path == root / "optional.env")
            self.assertFalse(source.required)
            with contextlib.redirect_stdout(io.StringIO()):
                restic = FakeRestic()
                BackupOrchestrator(FakeDocker(), restic, AppConfig(backup=BackupConfig(state_dir=root / "state"))).backup_group(group)
            self.assertNotIn(root / "optional.env", restic.backups[0][0])

    def test_explicit_exclusions_and_disabled_metadata_are_honored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.env"
            configs = [AppConfig(backup=BackupConfig(exclude_paths=(path,))),
                       AppConfig(projects={"app": ProjectConfig(exclude_paths=(path,))}),
                       AppConfig(backup=BackupConfig(include_compose_metadata=False))]
            for config in configs:
                with self.subTest(config=config):
                    group = self.group(root, "settings.env", config=config)
                    self.assertNotIn(path, {source.path for source in group.sources})

    def test_service_env_file_that_is_also_dotenv_is_still_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("TOKEN=value\n")
            group = self.group(root, ".env")
            sources = [source for source in group.sources if source.path == root / ".env"]
            self.assertEqual(len(sources), 1)
            self.assertTrue(sources[0].required)

    def test_ignored_service_environment_is_not_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(backup=BackupConfig(ignore_containers=("web",)))
            group = self.group(root, "missing.env", config=config)
            self.assertNotIn(root / "missing.env", {source.path for source in group.sources})

    def test_dynamic_paths_use_only_compose_interpolation_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.env"
            path.write_text("TOKEN=value\n")
            (root / ".env").write_text("ENV_FILE=settings.env\n")
            result = subprocess.CompletedProcess([], 0, stdout=json.dumps({"services": {"paths": {"environment": {"ENV_PATH_0": "settings.env"}}}}))
            with patch.dict(os.environ, {"ENV_FILE": "production.env"}), patch("backupdock.compose_metadata.run_process", return_value=result) as process:
                group = self.group(root, "${ENV_FILE:?ENV_FILE must be stored}")
            self.assertIn(path, {source.path for source in group.sources})
            self.assertNotIn("ENV_FILE", process.call_args.kwargs["env"])
            self.assertIn(str(root / ".env"), process.call_args.args[0])

    def test_interpolation_failure_is_a_discovery_error(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.CompletedProcess([], 1, stdout="", stderr="missing variable")
            with patch("backupdock.compose_metadata.run_process", return_value=result), self.assertRaisesRegex(DiscoveryError, "resolve service env_file"):
                self.group(Path(directory), "${ENV_FILE:?missing}")

    def test_backup_includes_service_env_file_and_symlink_contents_in_snapshot_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "shared.env"
            real.write_text("TOKEN=archived-value\n")
            settings = root / "settings.env"
            settings.symlink_to(real)
            group = self.group(root, "settings.env")
            config = AppConfig(backup=BackupConfig(state_dir=root / "state"))
            restic = FakeRestic()
            with contextlib.redirect_stdout(io.StringIO()):
                BackupOrchestrator(FakeDocker(), restic, config).backup_group(group)
            self.assertIn(real, restic.backups[0][0])
            self.assertIn(settings, restic.backups[0][0])
            metadata = json.loads((root / "state/manifests/compose_app.json").read_text())
            self.assertEqual(metadata["compose_file_aliases"][str(settings)], str(real))
            self.assertIn(str(real), {source["path"] for source in metadata["sources"]})


@unittest.skipUnless(shutil.which("docker"), "Docker CLI/Compose is not installed in this environment")
class ComposeEnvironmentIntegrationTests(unittest.TestCase):
    def test_discovered_service_env_file_can_be_restored_from_new_snapshot_without_live_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "shared.env"
            settings.write_text("TOKEN='archived$value'\n")
            (root / "settings.env").symlink_to(settings)
            compose = root / "compose.yaml"
            compose.write_text("services:\n  web:\n    image: example/app:1.0\n    env_file: ${ENV_FILE:?ENV_FILE must be stored}\n")
            (root / ".env").write_text("ENV_FILE=settings.env\n")
            instance = container("web", project="app", config_files=(str(compose),), working_dir=str(root))
            group = discover_groups([instance], AppConfig())[0]
            config = AppConfig(backup=BackupConfig(state_dir=root / "state"))
            restic = FakeRestic()
            with contextlib.redirect_stdout(io.StringIO()):
                BackupOrchestrator(FakeDocker(), restic, config).backup_group(group)
            archive = {str(path): path.read_text() for path in restic.backups[0][0] if path.is_file() and not path.is_symlink()}
            metadata = json.loads(archive[str(root / "state/manifests/compose_app.json")])
            settings.write_text("TOKEN=live-value\n")
            runner = MagicMock()
            runner._run.side_effect = lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout=archive[args[2]])
            model = TestRestore(runner, root / "restore-state")._prepare({"id": "a" * 64}, metadata, root / "restored", "app-test")
            self.assertEqual(model["services"]["web"]["environment"]["TOKEN"], "archived$$value")


if __name__ == "__main__":
    unittest.main()
