"""HTTP routes for container lifecycle, metrics, and snapshots.

The handlers are thin: they delegate to :class:`ContainerService` and map
domain exceptions to HTTP status codes. The service is resolved through
FastAPI dependency injection so tests can swap in a fake implementation.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from lxc_manager.api.deps import get_container_service
from lxc_manager.domain.models import ContainerCreate, ContainerMetrics, ContainerResponse
from lxc_manager.services.containers import (
    ContainerAlreadyExists,
    ContainerNotFound,
    ContainerNotRunning,
    LxcError,
    UnsupportedDistro,
)
from lxc_manager.services.containers import ContainerService

router = APIRouter()


@router.get("", response_model=list[ContainerResponse])
async def list_containers(
    service: ContainerService = Depends(get_container_service),
):
    """List all containers with their status."""
    try:
        return await service.list()
    except LxcError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))


@router.get("/{name}", response_model=ContainerResponse)
async def get_container(
    name: str, service: ContainerService = Depends(get_container_service)
):
    """Get details for a specific container."""
    try:
        return await service.get(name)
    except ContainerNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


@router.get("/{name}/metrics", response_model=ContainerMetrics)
async def get_metrics(
    name: str,
    include_snapshots: bool = Query(True),
    service: ContainerService = Depends(get_container_service),
):
    """Get resource metrics for a running container."""
    try:
        return await service.get_metrics(name, include_snapshots=include_snapshots)
    except ContainerNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except ContainerNotRunning as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.post(
    "", response_model=ContainerResponse, status_code=status.HTTP_201_CREATED
)
async def create_container(
    payload: ContainerCreate,
    service: ContainerService = Depends(get_container_service),
):
    """Create a new container from a base image."""
    try:
        return await service.create(payload)
    except ContainerAlreadyExists as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    except UnsupportedDistro as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except LxcError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))


@router.post("/{name}/start", response_model=ContainerResponse)
async def start_container(
    name: str, service: ContainerService = Depends(get_container_service)
):
    """Start a container."""
    try:
        return await service.start(name)
    except ContainerNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except LxcError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))


@router.post("/{name}/stop", response_model=ContainerResponse)
async def stop_container(
    name: str,
    force: bool = Query(False),
    service: ContainerService = Depends(get_container_service),
):
    """Stop a container."""
    try:
        return await service.stop(name, force=force)
    except ContainerNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except LxcError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))


@router.post("/{name}/restart", response_model=ContainerResponse)
async def restart_container(
    name: str, service: ContainerService = Depends(get_container_service)
):
    """Restart a container."""
    try:
        return await service.restart(name)
    except ContainerNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except LxcError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_container(
    name: str,
    force: bool = Query(False),
    service: ContainerService = Depends(get_container_service),
):
    """Delete a container."""
    try:
        await service.delete(name, force=force)
    except ContainerNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except LxcError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))
    return JSONResponse(status_code=204, content=None)


@router.post("/{name}/snapshot")
async def create_snapshot(
    name: str,
    snapshot_name: Optional[str] = None,
    service: ContainerService = Depends(get_container_service),
):
    """Create a snapshot of a container."""
    try:
        return await service.create_snapshot(name, snapshot_name)
    except ContainerNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except LxcError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))


@router.get("/{name}/snapshots")
async def list_snapshots(
    name: str, service: ContainerService = Depends(get_container_service)
):
    try:
        await service.get(name)
    except ContainerNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return {"snapshots": await service.list_snapshots(name)}


@router.delete("/{name}/snapshots/{snapshot_name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_snapshot(
    name: str,
    snapshot_name: str,
    service: ContainerService = Depends(get_container_service),
):
    """Delete a container snapshot."""
    try:
        await service.get(name)
    except ContainerNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    try:
        await service.delete_snapshot(name, snapshot_name)
    except LxcError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))
    return JSONResponse(status_code=204, content=None)


@router.get("/{name}/console")
async def get_console(
    name: str, service: ContainerService = Depends(get_container_service)
):
    """Get container console output (last 100 lines)."""
    try:
        await service.get(name)
    except ContainerNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return await service.get_console(name)
