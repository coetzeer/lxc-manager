"""LXC Manager package.

Provides a dependency-injected, testable FastAPI application for rootless
LXC container management. Use :func:`lxc_manager.app.create_app` to build an
application with your choice of configuration and service implementations.
"""

from .app import create_app, app  # noqa: F401

__version__ = "1.0.0"
