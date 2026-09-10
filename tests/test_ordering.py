from __future__ import annotations

import unittest

from backupdock.models import ContainerInfo
from backupdock.ordering import (
    DependencyOrderError,
    containers_in_start_order,
    running_containers_in_stop_order,
)


def container(name: str, dependencies: tuple[str, ...] = ()) -> ContainerInfo:
    return ContainerInfo(
        id=f"id-{name}",
        name=name,
        running=True,
        compose_project="app",
        compose_service=name,
        compose_working_dir=None,
        compose_config_files=(),
        compose_environment_files=(),
        mounts=(),
        compose_dependencies=dependencies,
    )


class OrderingTests(unittest.TestCase):
    def test_dependencies_start_first_and_stop_last(self) -> None:
        containers = [
            container("web", ("db", "redis")),
            container("redis"),
            container("db"),
        ]

        self.assertEqual(
            [item.name for item in containers_in_start_order(containers)],
            ["db", "redis", "web"],
        )
        self.assertEqual(
            [item.name for item in running_containers_in_stop_order(containers)],
            ["web", "redis", "db"],
        )

    def test_stopped_containers_are_not_returned_for_stop(self) -> None:
        db = container("db")
        web = container("web", ("db",))
        db = ContainerInfo(
            id=db.id,
            name=db.name,
            running=False,
            compose_project=db.compose_project,
            compose_service=db.compose_service,
            compose_working_dir=db.compose_working_dir,
            compose_config_files=db.compose_config_files,
            compose_environment_files=db.compose_environment_files,
            mounts=db.mounts,
            compose_dependencies=db.compose_dependencies,
        )

        self.assertEqual(
            [item.name for item in running_containers_in_stop_order([web, db])],
            ["web"],
        )

    def test_dependency_cycle_fails(self) -> None:
        containers = [
            container("a", ("b",)),
            container("b", ("a",)),
        ]

        with self.assertRaisesRegex(DependencyOrderError, "dependency cycle"):
            containers_in_start_order(containers)


if __name__ == "__main__":
    unittest.main()
