from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

from backupdock.backup import BackupOrchestrator
from backupdock.config import AppConfig, BackupConfig
from backupdock.discovery import DiscoveryError, discover_groups
from backupdock.docker_backend import DockerBackend
from backupdock.models import ContainerInfo, MountInfo
from backupdock.volumes import resolve_volume_mount


def container(name, mount, *, project="app"):
    return ContainerInfo(id=f"id-{name}", name=name, running=True, compose_project=project,
                         compose_service=name, compose_working_dir=None, compose_config_files=(),
                         compose_environment_files=(), mounts=(mount,))


class VolumeSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.device = self.root / "data"
        self.device.mkdir()
        self.mountpoint = self.root / "docker" / "volumes" / "app_data" / "_data"
        self.mountpoint.mkdir(parents=True)
        self.mount = MountInfo("volume", str(self.mountpoint), "/data", "app_data")
        self.attrs = {"Name": "app_data", "Driver": "local", "Mountpoint": str(self.mountpoint),
                      "Options": {"device": str(self.device), "o": "bind"}}

    def resolve(self, **attrs):
        return resolve_volume_mount(self.mount, {**self.attrs, **attrs})

    def test_bind_volume_backup_reads_payload_after_mountpoint_becomes_empty(self):
        content = b"persistent database contents"
        (self.device / "database.db").write_bytes(content)
        (self.mountpoint / "database.db").write_bytes(content)
        mount = self.resolve()
        groups = discover_groups([container("db", mount)], AppConfig())
        events = []
        captured = {}
        mountpoint = self.mountpoint

        class Docker:
            def stop(self, container_id):
                events.append("stop")
                (mountpoint / "database.db").unlink()
            def ensure_running(self, container_id):
                events.append("restart")

        class Restic:
            def backup(self, paths, group_key, **kwargs):
                events.append("backup")
                for path in paths:
                    if path.is_dir():
                        for child in path.rglob("*"):
                            if child.is_file():
                                captured[child.name] = child.read_bytes()
                    elif path.is_file():
                        captured[path.name] = path.read_bytes()

        with contextlib.redirect_stdout(io.StringIO()):
            BackupOrchestrator(Docker(), Restic(), AppConfig(backup=BackupConfig(state_dir=self.root / "state"))).backup_group(groups[0])
        self.assertEqual(events, ["stop", "backup", "restart"])
        self.assertEqual(list(self.mountpoint.iterdir()), [])
        self.assertEqual(captured["database.db"], content)
        self.assertEqual(groups[0].sources[0].volume_name, "app_data")
        self.assertEqual(mount.source, str(self.mountpoint))

    def test_plain_local_volume_keeps_native_mountpoint(self):
        for options in [None, {}, {"size": "1G"}]:
            with self.subTest(options=options):
                mount = self.resolve(Options=options)
                self.assertIsNone(mount.backup_error)
                self.assertEqual(discover_groups([container("db", mount)], AppConfig())[0].sources[0].path, self.mountpoint)

    def test_bind_options_accept_empty_or_none_type_and_preserve_readonly(self):
        for mount_type in ["", "none"]:
            mount = resolve_volume_mount(replace(self.mount, read_only=True), {**self.attrs, "Options": {"device": str(self.device), "type": mount_type, "o": "bind,ro"}})
            self.assertIsNone(mount.backup_error)
            self.assertEqual(mount.backup_source, str(self.device))
            self.assertTrue(mount.read_only)

    def test_bind_subdirectory_and_symlink_device_resolve_to_stable_data(self):
        (self.device / "db").mkdir()
        alias = self.root / "alias"
        alias.symlink_to(self.device, target_is_directory=True)
        mount = resolve_volume_mount(replace(self.mount, source=str(self.mountpoint / "db")), {**self.attrs, "Options": {"device": str(alias), "o": "rbind"}})
        self.assertEqual(mount.backup_source, str(self.device / "db"))

    def test_invalid_or_escaping_volume_source_fails_discovery(self):
        for source in ["", "relative", str(self.mountpoint / ".." / "outside")]:
            with self.subTest(source=source):
                mount = resolve_volume_mount(replace(self.mount, source=source), self.attrs)
                self.assertIsNotNone(mount.backup_error)
                with self.assertRaises(DiscoveryError):
                    discover_groups([container("db", mount)], AppConfig())

    def test_subdirectory_symlink_cannot_escape_bind_source(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.device / "db").symlink_to(outside, target_is_directory=True)
        mount = resolve_volume_mount(replace(self.mount, source=str(self.mountpoint / "db")), self.attrs)
        self.assertIsNotNone(mount.backup_error)

    def test_aliases_across_groups_fail_before_backup(self):
        first = self.resolve()
        other = replace(first, volume_name="other_data", source="/another/docker/mountpoint")
        with self.assertRaises(DiscoveryError):
            discover_groups([container("db", first, project="alpha"), container("web", other, project="beta")], AppConfig())

    def test_original_and_stable_exclusion_paths_are_both_honored(self):
        for path in [self.mountpoint, self.device]:
            groups = discover_groups([container("db", self.resolve())], AppConfig(backup=BackupConfig(exclude_paths=(path,))))
            self.assertEqual(groups[0].sources, [])

    def test_unsupported_volume_fails_discovery_but_explicit_name_exclusion_works(self):
        for attrs in [{"Driver": "plugin"}, {"Options": {"type": "nfs", "device": ":/data", "o": "addr=example"}},
                      {"Options": {"type": "ext4", "device": "/dev/example"}}, {"Options": {"device": "relative", "o": "bind"}},
                      {"Name": "wrong"}, {"Options": {"unexpected": "value"}}]:
            with self.subTest(attrs=attrs):
                mount = self.resolve(**attrs)
                with self.assertRaises(DiscoveryError):
                    discover_groups([container("db", mount)], AppConfig())
                groups = discover_groups([container("db", mount)], AppConfig(backup=BackupConfig(exclude_volumes=("app_data",))))
                self.assertEqual(groups[0].sources, [])

    def test_ignored_writable_volume_uses_stable_path_for_safety_check(self):
        ignored = container("worker", self.resolve(), project="worker")
        regular = container("db", MountInfo("bind", str(self.device), "/data"))
        config = AppConfig(backup=BackupConfig(ignore_containers=("worker",)))
        groups = discover_groups([ignored, regular], config)
        docker = MagicMock()
        restic = MagicMock()
        with self.assertRaisesRegex(RuntimeError, "Ignored running containers"), contextlib.redirect_stdout(io.StringIO()):
            BackupOrchestrator(docker, restic, config).run(groups, [])
        docker.stop.assert_not_called()
        restic.preflight.assert_not_called()

    def test_unknown_ignored_writable_driver_does_not_bypass_safety(self):
        mount = self.resolve(Driver="plugin")
        with self.assertRaises(DiscoveryError):
            discover_groups([container("worker", mount)], AppConfig(backup=BackupConfig(ignore_containers=("worker",))))

    def test_backend_inspects_shared_volume_once_and_refreshes_each_listing(self):
        backend = DockerBackend.__new__(DockerBackend)
        backend._client = MagicMock()
        attrs = {"Id": "id-db", "Name": "/db", "State": {"Running": True}, "Config": {"Labels": {}},
                 "Mounts": [{"Type": "volume", "Source": str(self.mountpoint), "Destination": "/data", "Name": "app_data", "RW": True}]}
        backend._client.containers.list.return_value = [MagicMock(attrs=attrs), MagicMock(attrs={**attrs, "Id": "id-web", "Name": "/web"})]
        backend._client.volumes.get.return_value.attrs = self.attrs
        result = backend.list_containers()
        self.assertEqual({item.mounts[0].backup_source for item in result}, {str(self.device)})
        backend._client.volumes.get.assert_called_once_with("app_data")
        backend.list_containers()
        self.assertEqual(backend._client.volumes.get.call_count, 2)

    def test_inspection_failure_is_not_silently_treated_as_plain_local_volume(self):
        backend = DockerBackend.__new__(DockerBackend)
        backend._client = MagicMock()
        attrs = {"Id": "id-db", "Name": "/db", "State": {"Running": True}, "Config": {"Labels": {}},
                 "Mounts": [{"Type": "volume", "Source": str(self.mountpoint), "Destination": "/data", "Name": "app_data"}]}
        backend._client.containers.list.return_value = [MagicMock(attrs=attrs)]
        backend._client.volumes.get.side_effect = RuntimeError("inspect failed")
        result = backend.list_containers()
        with self.assertRaises(DiscoveryError):
            discover_groups(result, AppConfig())


if __name__ == "__main__":
    unittest.main()
