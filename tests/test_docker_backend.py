from __future__ import annotations

import unittest

from backupdock.docker_backend import merge_runtime_dependencies, parse_container_attrs


class DockerBackendTests(unittest.TestCase):
    def test_parses_compose_dependency_label(self) -> None:
        attrs = {
            "Id": "web-id",
            "Name": "/app-web-1",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "app",
                    "com.docker.compose.service": "web",
                    "com.docker.compose.depends_on": "db:service_healthy:false,redis:service_started:false",
                }
            },
            "Mounts": [],
        }

        container = parse_container_attrs(attrs)

        self.assertEqual(container.compose_dependencies, ("db", "redis"))

    def test_merges_runtime_links_volumes_from_and_network_mode_dependencies(self) -> None:
        db_attrs = {
            "Id": "db-id-1234567890",
            "Name": "/app-db-1",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "app",
                    "com.docker.compose.service": "db",
                }
            },
            "HostConfig": {},
            "Mounts": [],
        }
        redis_attrs = {
            "Id": "redis-id-1234567890",
            "Name": "/app-redis-1",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "app",
                    "com.docker.compose.service": "redis",
                }
            },
            "HostConfig": {},
            "Mounts": [],
        }
        helper_attrs = {
            "Id": "helper-id-1234567890",
            "Name": "/app-helper-1",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "app",
                    "com.docker.compose.service": "helper",
                }
            },
            "HostConfig": {},
            "Mounts": [],
        }
        web_attrs = {
            "Id": "web-id-1234567890",
            "Name": "/app-web-1",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "app",
                    "com.docker.compose.service": "web",
                }
            },
            "HostConfig": {
                "Links": ["/app-db-1:/app-web-1/db"],
                "VolumesFrom": ["app-redis-1:ro"],
                "NetworkMode": "container:helper-id-1234567890",
            },
            "Mounts": [],
        }

        attrs_list = [db_attrs, redis_attrs, helper_attrs, web_attrs]
        containers = [parse_container_attrs(attrs) for attrs in attrs_list]
        enriched = merge_runtime_dependencies(containers, attrs_list)
        web = next(container for container in enriched if container.compose_service == "web")

        self.assertEqual(web.compose_dependencies, ("db", "helper", "redis"))

    def test_runtime_dependency_from_other_project_is_ignored(self) -> None:
        external_attrs = {
            "Id": "external-id",
            "Name": "/external-db-1",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "other",
                    "com.docker.compose.service": "db",
                }
            },
            "HostConfig": {},
            "Mounts": [],
        }
        app_attrs = {
            "Id": "app-id",
            "Name": "/app-web-1",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "app",
                    "com.docker.compose.service": "web",
                }
            },
            "HostConfig": {"NetworkMode": "container:external-id"},
            "Mounts": [],
        }

        attrs_list = [external_attrs, app_attrs]
        containers = [parse_container_attrs(attrs) for attrs in attrs_list]
        enriched = merge_runtime_dependencies(containers, attrs_list)
        web = next(container for container in enriched if container.compose_service == "web")

        self.assertEqual(web.compose_dependencies, ())


if __name__ == "__main__":
    unittest.main()
