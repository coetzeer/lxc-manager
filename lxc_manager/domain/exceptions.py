"""Domain exceptions used by the service layer.

These are intentionally free of FastAPI so the same exceptions can be raised by
a CLI, a background worker, or the HTTP layer, which then maps them to status
codes.
"""


class LxcError(Exception):
    """Base exception for LXC operations."""


class ContainerNotFound(LxcError):
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"Container '{name}' not found")


class ContainerAlreadyExists(LxcError):
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"Container '{name}' already exists")


class ContainerNotRunning(LxcError):
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"Container '{name}' is not running")


class UnsupportedDistro(LxcError):
    def __init__(self, distro: str):
        self.distro = distro
        super().__init__(f"Unsupported distribution '{distro}'")


class ImageCatalogUnavailable(LxcError):
    """Raised when the upstream LXC image catalog cannot be fetched."""
