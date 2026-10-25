"""Shared domain models, independent of FastAPI/HTTP.

Pydantic request/response models and plain dataclasses live here so they can be
imported and asserted on without constructing a test client.
"""
from dataclasses import dataclass
from typing import List, Optional

from pydantic import BaseModel, Field


# Supported LXC image distributions (from lxc-launch.sh)
DISTRO_IMAGES = {
    "almalinux": {"release": "10", "alias": "alma"},
    "alpine": {"release": "3.24", "alias": "alpine"},
    "alt": {"release": "p11", "alias": "alt"},
    "amazonlinux": {"release": "2023", "alias": "amazonlinux"},
    "archlinux": {"release": "current", "alias": "archlinux"},
    "busybox": {"release": "1.38.0", "alias": "busybox"},
    "centos": {"release": "10-Stream", "alias": "centos"},
    "debian": {"release": "bookworm", "alias": "debian"},
    "devuan": {"release": "daedalus", "alias": "devuan"},
    "fedora": {"release": "44", "alias": "fedora"},
    "kali": {"release": "current", "alias": "kali"},
    "mint": {"release": "zena", "alias": "mint"},
    "nixos": {"release": "26.05", "alias": "nixos"},
    "openeuler": {"release": "24.03", "alias": "openeuler"},
    "opensuse": {"release": "16.0", "alias": "opensuse"},
    "openwrt": {"release": "25.12", "alias": "openwrt"},
    "oracle": {"release": "10", "alias": "oracle"},
    "plamo": {"release": "8.x", "alias": "plamo"},
    "rockylinux": {"release": "10", "alias": "rocky"},
    "slackware": {"release": "15.0", "alias": "slackware"},
    "ubuntu": {"release": "resolute", "alias": "ubuntu"},
    "voidlinux": {"release": "current", "alias": "void"},
}


class ContainerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    distro: str = Field(..., description="Base image distribution")
    release: Optional[str] = Field(
        None, description="Specific release (uses default if omitted)"
    )
    arch: str = Field("amd64", pattern=r"^(amd64|arm64|i386)$")


class ContainerAction(BaseModel):
    name: str


class ContainerResponse(BaseModel):
    name: str
    status: str
    pid: Optional[int] = None
    ipv4: Optional[str] = None
    ipv6: Optional[str] = None


class ContainerMetrics(BaseModel):
    name: str
    cpu_usage_percent: Optional[float] = None
    memory_usage_bytes: Optional[int] = None
    memory_limit_bytes: Optional[int] = None
    disk_read_bytes: Optional[int] = None
    disk_write_bytes: Optional[int] = None
    network_rx_bytes: Optional[int] = None
    network_tx_bytes: Optional[int] = None
    snapshots: List[str] = []


class LoginCredentials(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=4096)


@dataclass(frozen=True)
class Principal:
    username: str
    uid: int
    gid: int
    home: str
    shell: str


@dataclass
class SessionRecord:
    principal: Principal
    csrf_hash: str
    created_at: float
    last_seen: float
    expires_at: float


@dataclass
class LoginThrottle:
    failures: int = 0
    retry_after: float = 0.0
