"""FastAPI application factory.

Builds an application with the container service wired into application state
so route handlers can resolve it through dependency injection (and tests can
override it).
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from lxc_manager.api.routes.containers import router as containers_router
from lxc_manager.config import LxcConfig
from lxc_manager.services.containers import ContainerService
from lxc_manager.services.lxc import SubprocessLxcClient

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify rootless LXC setup on startup."""
    lxc: SubprocessLxcClient = app.state.lxc_client
    result = await lxc.run_lxc("ls", "-f", "-F", "NAME")
    if result.returncode != 0:
        print(f"Warning: LXC not accessible: {result.stderr}")
    else:
        print("LXC rootless setup verified")
    yield


def create_app(config: LxcConfig | None = None) -> FastAPI:
    """Create and wire up the FastAPI application.

    Parameters
    ----------
    config:
        Optional configuration. Defaults to reading ``LXC_MANAGER_*`` env vars.
        Pass a custom :class:`LxcConfig` in tests to avoid environment coupling.
    """
    config = config or LxcConfig.from_env()

    app = FastAPI(
        title="LXC Manager API",
        description="Rootless LXC container management via FastAPI (LXC 7.0 CLI)",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.mount(
        "/static",
        StaticFiles(directory=STATIC_DIR, check_dir=False),
        name="static",
    )

    lxc_client = SubprocessLxcClient(config)
    container_service = ContainerService(lxc_client, config)

    app.state.lxc_client = lxc_client
    app.state.container_service = container_service
    app.state.config = config

    app.include_router(containers_router, prefix="/containers")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8083)
