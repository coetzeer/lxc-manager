"""FastAPI dependencies that resolve services from application state."""

from typing import cast

from fastapi import Request

from lxc_manager.services.containers import ContainerService


async def get_container_service(request: Request) -> ContainerService:
    """Return the :class:`ContainerService` stored on the app state.

    Tests replace this via ``app.dependency_overrides`` to inject a fake
    service without touching subprocesses.
    """
    service = request.app.state.container_service
    return cast(ContainerService, service)
