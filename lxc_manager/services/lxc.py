"""LXC client abstraction: protocol + subprocess implementation.

The :class:`LxcClient` protocol lets tests inject a fake client instead of
spawning real ``lxc-*`` processes. :class:`SubprocessLxcClient` is the
production implementation that shells out to the CLI binaries.
"""
import asyncio
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from lxc_manager.config import LxcConfig


@dataclass(frozen=True)
class LxcResult:
    returncode: int
    stdout: str
    stderr: str


@runtime_checkable
class LxcClient(Protocol):
    async def run(self, *args: str, timeout: int = 30) -> LxcResult: ...

    async def run_lxc(
        self, subcommand: str, *args: str, timeout: int = 30
    ) -> LxcResult: ...

    async def run_attach(
        self, name: str, *args: str, timeout: int = 30
    ) -> LxcResult: ...


class SubprocessLxcClient:
    """Real implementation that shells out to the ``lxc-*`` CLI binaries."""

    def __init__(self, config: LxcConfig):
        self.config = config
        self._lxc_dir = config.lxc_path

    async def run(self, *args: str, timeout: int = 30) -> LxcResult:
        """Run a noninteractive command without a controlling terminal."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return LxcResult(
                proc.returncode,
                stdout.decode().strip(),
                stderr.decode().strip(),
            )
        except asyncio.TimeoutError:
            return LxcResult(-1, "", f"Command timed out after {timeout}s")
        except FileNotFoundError:
            return LxcResult(-1, "", f"Command not found: {args[0]}")
        except Exception as exc:  # noqa: BLE001 - surface as a failed result
            return LxcResult(-1, "", str(exc))

    async def run_lxc(
        self, subcommand: str, *args: str, timeout: int = 30
    ) -> LxcResult:
        """Run an ``lxc-{subcommand}`` command."""
        return await self.run(
            f"{self._lxc_dir}/lxc-{subcommand}", *args, timeout=timeout
        )

    async def run_attach(
        self, name: str, *args: str, timeout: int = 30
    ) -> LxcResult:
        """Run a command inside a container via ``lxc-attach``."""
        return await self.run(
            f"{self._lxc_dir}/lxc-attach", "-n", name, "--", *args, timeout=timeout
        )
