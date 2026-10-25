"""Container business logic.

The service layer knows nothing about FastAPI or HTTP. It raises domain
exceptions which the HTTP layer maps to status codes. This makes the same logic
reusable by the API, a CLI, or background workers, and trivially testable with
a fake :class:`lxc_manager.services.lxc.LxcClient`.
"""
import asyncio
import ipaddress
import re
import time
from typing import List, Optional

from lxc_manager.config import LxcConfig
from lxc_manager.domain.exceptions import (
    ContainerAlreadyExists,
    ContainerNotFound,
    ContainerNotRunning,
    LxcError,
    UnsupportedDistro,
)
from lxc_manager.domain.models import (
    ContainerCreate,
    ContainerMetrics,
    ContainerResponse,
    DISTRO_IMAGES,
)
from lxc_manager.services.lxc import LxcClient, LxcResult


def parse_lxc_ls_output(output: str) -> List[ContainerResponse]:
    """Parse ``lxc-ls -f -F NAME,STATE,PID,IPV4,IPV6`` output."""
    containers: List[ContainerResponse] = []
    lines = output.strip().split("\n")
    if len(lines) < 2:
        return containers

    columns = [(match.group(), match.start()) for match in re.finditer(r"\S+", lines[0])]
    if not {"NAME", "STATE", "PID", "IPV4", "IPV6"}.issubset(
        key for key, _ in columns
    ):
        return containers

    for line in lines[1:]:
        if not line.strip():
            continue
        fields = {
            key: line[
                start : columns[index + 1][1] if index + 1 < len(columns) else len(line)
            ].strip()
            for index, (key, start) in enumerate(columns)
        }
        if fields["NAME"] and fields["STATE"]:
            containers.append(
                ContainerResponse(
                    name=fields["NAME"],
                    status=fields["STATE"],
                    pid=int(fields["PID"]) if fields["PID"].isdigit() else None,
                    ipv4=fields["IPV4"] if fields["IPV4"] not in ("-", "") else None,
                    ipv6=fields["IPV6"] if fields["IPV6"] not in ("-", "") else None,
                )
            )
    return containers


class ContainerService:
    """Stateless service for container lifecycle, metrics, and snapshots."""

    def __init__(self, lxc: LxcClient, config: LxcConfig):
        self.lxc = lxc
        self.config = config

    # -- lifecycle ---------------------------------------------------------

    async def list(self) -> List[ContainerResponse]:
        result: LxcResult = await self.lxc.run_lxc(
            "ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6"
        )
        if result.returncode != 0:
            raise LxcError(result.stderr or "Failed to list containers")
        return parse_lxc_ls_output(result.stdout)

    async def get(self, name: str) -> ContainerResponse:
        containers = await self.list()
        for container in containers:
            if container.name == name:
                return container
        raise ContainerNotFound(name)

    async def _exists(self, name: str) -> bool:
        try:
            await self.get(name)
            return True
        except ContainerNotFound:
            return False

    async def create(self, payload: ContainerCreate) -> ContainerResponse:
        distro_lower = payload.distro.lower()
        if distro_lower not in DISTRO_IMAGES:
            raise UnsupportedDistro(distro_lower)
        if await self._exists(payload.name):
            raise ContainerAlreadyExists(payload.name)

        distro_info = DISTRO_IMAGES[distro_lower]
        release = payload.release or distro_info["release"]

        result = await self.lxc.run_lxc(
            "create",
            "-n",
            payload.name,
            "-t",
            "download",
            "--",
            "--dist",
            distro_lower,
            "--release",
            release,
            "--arch",
            payload.arch,
            timeout=180,
        )
        if result.returncode != 0:
            raise LxcError(result.stderr or "Failed to create container")

        return await self.get(payload.name)

    async def start(self, name: str) -> ContainerResponse:
        container = await self.get(name)
        if container.status == "RUNNING":
            return container

        result = await self.lxc.run_lxc("start", "-n", name, timeout=30)
        if result.returncode != 0:
            raise LxcError(result.stderr or "Failed to start container")

        await asyncio.sleep(1)
        return await self.get(name)

    async def stop(self, name: str, force: bool = False) -> ContainerResponse:
        container = await self.get(name)
        if container.status != "RUNNING":
            return container

        args = ["stop", "-n", name]
        if force:
            args.insert(1, "-k")

        result = await self.lxc.run_lxc(*args, timeout=30)
        if result.returncode != 0:
            raise LxcError(result.stderr or "Failed to stop container")

        await asyncio.sleep(1)
        return await self.get(name)

    async def restart(self, name: str) -> ContainerResponse:
        container = await self.get(name)

        if container.status == "RUNNING":
            result = await self.lxc.run_lxc("stop", "-n", name, timeout=30)
            if result.returncode != 0:
                raise LxcError(
                    result.stderr or "Failed to stop container for restart"
                )

        result = await self.lxc.run_lxc("start", "-n", name, timeout=30)
        if result.returncode != 0:
            raise LxcError(result.stderr or "Failed to restart container")

        await asyncio.sleep(1)
        return await self.get(name)

    async def delete(self, name: str, force: bool = False) -> None:
        container = await self.get(name)

        if container.status == "RUNNING":
            result = await self.lxc.run_lxc(
                "stop", "-n", name, "-k", timeout=30
            )
            if result.returncode != 0:
                raise LxcError(
                    result.stderr or "Failed to stop container before deletion"
                )
            await asyncio.sleep(1)

        args = ["destroy", "-n", name]
        if force:
            args.append("-f")

        result = await self.lxc.run_lxc(*args, timeout=30)
        if result.returncode != 0:
            raise LxcError(result.stderr or "Failed to delete container")

    # -- networking / metrics ---------------------------------------------

    async def get_ipv4(self, name: str) -> str:
        """Read a container's primary IPv4 address from its network namespace."""
        result = await self.lxc.run_attach(
            name,
            "sh",
            "-c",
            "ip -4 -o addr show scope global | awk '$2 != \"lo\" {sub(/\\/.*/, \"\", $4); print $4; exit}'",
            timeout=10,
        )
        if result.returncode != 0:
            raise LxcError(
                f"Unable to determine the container address: {result.stderr}"
            )
        try:
            address = ipaddress.ip_address(result.stdout.strip())
        except ValueError as error:
            raise LxcError(
                "The container has no usable IPv4 address. Check its network setup."
            ) from error
        if not address.is_private:
            raise LxcError(
                "The container address is not a private IPv4 address."
            )
        return str(address)

    async def get_metrics(
        self, name: str, include_snapshots: bool = True
    ) -> ContainerMetrics:
        """Get resource metrics for a running container."""
        container = await self.get(name)
        if container.status != "RUNNING":
            raise ContainerNotRunning(name)

        metrics = ContainerMetrics(name=name)

        started = time.monotonic()
        result, _ = await self._read_cpu_stat(name)
        if result is not None:
            first = int(dict(line.split() for line in result.splitlines())["usage_usec"])
            await asyncio.sleep(0.2)
            second_result, _ = await self._read_cpu_stat(name)
            elapsed = time.monotonic() - started
            if second_result is not None:
                try:
                    second = int(
                        dict(line.split() for line in second_result.splitlines())[
                            "usage_usec"
                        ]
                    )
                    metrics.cpu_usage_percent = max(
                        0.0, (second - first) / (elapsed * 10000)
                    )
                except (ValueError, IndexError, KeyError):
                    pass

        result, _ = await self._read_file(name, "/sys/fs/cgroup/memory.current")
        if result is not None:
            try:
                metrics.memory_usage_bytes = int(result.strip())
            except ValueError:
                pass

        result, _ = await self._read_file(name, "/sys/fs/cgroup/memory.max")
        if result is not None and result.strip() != "max":
            try:
                metrics.memory_limit_bytes = int(result.strip())
            except ValueError:
                pass

        result, _ = await self._read_file(name, "/sys/fs/cgroup/io.stat")
        if result is not None and result:
            totals = {"rbytes": 0, "wbytes": 0}
            for line in result.splitlines():
                for field in line.split()[1:]:
                    key, _, value = field.partition("=")
                    if key in totals:
                        try:
                            totals[key] += int(value)
                        except ValueError:
                            pass
            metrics.disk_read_bytes = totals["rbytes"]
            metrics.disk_write_bytes = totals["wbytes"]

        result, _ = await self._read_file(name, "/proc/net/dev")
        if result is not None:
            for line in result.splitlines()[2:]:
                parts = line.split()
                if len(parts) >= 10 and parts[0].rstrip(":") != "lo":
                    try:
                        metrics.network_rx_bytes = (
                            (metrics.network_rx_bytes or 0) + int(parts[1])
                        )
                        metrics.network_tx_bytes = (
                            (metrics.network_tx_bytes or 0) + int(parts[9])
                        )
                    except (ValueError, IndexError):
                        pass

        if include_snapshots:
            metrics.snapshots = await self.list_snapshots(name)
        return metrics

    async def _read_cpu_stat(
        self, name: str
    ) -> tuple[Optional[str], Optional[str]]:
        result = await self.lxc.run_attach(
            name, "cat", "/sys/fs/cgroup/cpu.stat", timeout=5
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout, result.stderr
        return None, result.stderr

    async def _read_file(
        self, name: str, path: str
    ) -> tuple[Optional[str], Optional[str]]:
        result = await self.lxc.run_attach(name, "cat", path, timeout=5)
        if result.returncode == 0 and result.stdout:
            return result.stdout, result.stderr
        return None, result.stderr

    # -- snapshots ----------------------------------------------------------

    async def list_snapshots(self, name: str) -> List[str]:
        result = await self.lxc.run_lxc("snapshot", "-n", name, "-L")
        if result.returncode != 0:
            raise LxcError(result.stderr or "Failed to list snapshots")

        snapshots: List[str] = []
        if result.returncode == 0 and result.stdout:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line and not line.startswith("Snapshots"):
                    snap_name = line.split()[0]
                    if snap_name:
                        snapshots.append(snap_name)
        return snapshots

    async def create_snapshot(
        self, name: str, snapshot_name: Optional[str] = None
    ) -> dict:
        args = ["snapshot", "-n", name]
        if snapshot_name:
            args.extend(["-c", snapshot_name])

        result = await self.lxc.run_lxc(*args, timeout=30)
        if result.returncode != 0:
            raise LxcError(result.stderr or "Failed to create snapshot")

        return {"message": "Snapshot created", "output": result.stdout}

    async def delete_snapshot(self, name: str, snapshot_name: str) -> None:
        result = await self.lxc.run_lxc(
            "snapshot", "-n", name, "-d", snapshot_name, timeout=10
        )
        if result.returncode != 0:
            raise LxcError(result.stderr or "Failed to delete snapshot")

    # -- console ------------------------------------------------------------

    async def get_console(self, name: str) -> dict:
        """Get container console output (last 100 lines)."""
        result = await self.lxc.run_attach(
            name, "journalctl", "-n", "100", "--no-pager", timeout=10
        )
        if result.returncode != 0:
            result = await self.lxc.run_attach(name, "dmesg", "-T", timeout=10)
        return {"console": result.stdout or result.stderr}
