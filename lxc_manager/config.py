"""Application configuration read from environment variables.

Kept free of FastAPI so it can be instantiated and asserted on in unit tests
without spinning up the web framework.
"""
import os
from dataclasses import dataclass
from typing import FrozenSet


@dataclass(frozen=True)
class LxcConfig:
    """Runtime configuration for the LXC Manager application."""

    lxc_path: str = "/usr/bin"
    pam_service: str = "lxc-manager"
    auth_idle_seconds: int = 30 * 60
    auth_max_age_seconds: int = 8 * 60 * 60
    auth_cookie_secure: bool = False
    allowed_users: FrozenSet[str] = frozenset()
    allowed_groups: FrozenSet[str] = frozenset()
    shell_idle_timeout: int = 30 * 60
    shell_reconnect_grace: int = 60
    default_shell: str = "/bin/sh"

    @classmethod
    def from_env(cls) -> "LxcConfig":
        """Build config from ``LXC_MANAGER_*`` environment variables."""

        def _csv(name: str) -> FrozenSet[str]:
            return frozenset(
                value.strip()
                for value in os.environ.get(name, "").split(",")
                if value.strip()
            )

        return cls(
            lxc_path=os.environ.get("LXC_MANAGER_LXC_PATH", "/usr/bin"),
            pam_service=os.environ.get("LXC_MANAGER_PAM_SERVICE", "lxc-manager"),
            auth_idle_seconds=int(
                os.environ.get("LXC_MANAGER_AUTH_IDLE_SECONDS", str(30 * 60))
            ),
            auth_max_age_seconds=int(
                os.environ.get("LXC_MANAGER_AUTH_MAX_AGE_SECONDS", str(8 * 60 * 60))
            ),
            auth_cookie_secure=os.environ.get(
                "LXC_MANAGER_AUTH_COOKIE_SECURE", "0"
            )
            == "1",
            allowed_users=_csv("LXC_MANAGER_ALLOWED_USERS"),
            allowed_groups=_csv("LXC_MANAGER_ALLOWED_GROUPS"),
        )
