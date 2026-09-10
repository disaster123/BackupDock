from __future__ import annotations

from collections import defaultdict

from backupdock.models import ContainerInfo


class DependencyOrderError(RuntimeError):
    pass


def _service_name(container: ContainerInfo) -> str:
    return container.compose_service or container.name


def service_start_order(containers: list[ContainerInfo]) -> list[str]:
    services = sorted({_service_name(container) for container in containers})
    known = set(services)
    dependencies: dict[str, set[str]] = {service: set() for service in services}

    for container in containers:
        service = _service_name(container)
        dependencies[service].update(
            dependency
            for dependency in container.compose_dependencies
            if dependency in known and dependency != service
        )

    dependents: dict[str, set[str]] = defaultdict(set)
    indegree: dict[str, int] = {}
    for service in services:
        indegree[service] = len(dependencies[service])
        for dependency in dependencies[service]:
            dependents[dependency].add(service)

    ready = sorted(service for service in services if indegree[service] == 0)
    ordered: list[str] = []

    while ready:
        service = ready.pop(0)
        ordered.append(service)
        for dependent in sorted(dependents.get(service, ())):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort()

    if len(ordered) != len(services):
        cyclic = sorted(service for service in services if indegree[service] > 0)
        raise DependencyOrderError(
            "Compose dependency cycle detected between services: " + ", ".join(cyclic)
        )

    return ordered


def containers_in_start_order(containers: list[ContainerInfo]) -> list[ContainerInfo]:
    by_service: dict[str, list[ContainerInfo]] = defaultdict(list)
    for container in containers:
        by_service[_service_name(container)].append(container)

    ordered: list[ContainerInfo] = []
    for service in service_start_order(containers):
        ordered.extend(sorted(by_service[service], key=lambda container: container.name))
    return ordered


def running_containers_in_stop_order(containers: list[ContainerInfo]) -> list[ContainerInfo]:
    return [
        container
        for container in reversed(containers_in_start_order(containers))
        if container.running and not container.ignored
    ]
