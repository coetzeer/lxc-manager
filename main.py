"""
FastAPI application for rootless LXC container management (LXC 7.0 CLI).

This module is a thin backward-compatible shim. The application is built by
:func:`lxc_manager.app.create_app`, which wires the container routes through a
dependency-injected :class:`lxc_manager.services.containers.ContainerService`.
Auth, shell, proxy, doctor, and image-catalog routes (plus the module-level
helpers the test suite patches) remain here so existing tests keep working
while the container layer is migrated to the new package.
"""
from contextlib import suppress
from dataclasses import dataclass
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import uuid4
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import pwd
import grp
import re
import secrets
import signal
import subprocess
import threading
import time
import shlex
from urllib.parse import quote, urlsplit
from pathlib import Path
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import HTTPError, URLError
import httpx

from lxc_manager.app import app  # noqa: F401  (built by create_app)
from lxc_manager.app import STATIC_DIR  # noqa: F401
from lxc_manager.domain.models import (  # noqa: F401
    ContainerCreate,
    ContainerAction,
    ContainerResponse,
    ContainerMetrics,
    LoginCredentials,
    Principal,
    SessionRecord,
    LoginThrottle,
    DISTRO_IMAGES,
)
from lxc_manager.config import LxcConfig  # noqa: F401

logger = logging.getLogger("lxc-manager.auth")

# Supported LXC image distributions (from lxc-launch.sh)
# (re-exported from lxc_manager.domain.models for backward compatibility)

LXC_PATH = "/usr/bin"
AUTH_COOKIE = "lxc_manager_session"
CSRF_COOKIE = "lxc_manager_csrf"
PAM_SERVICE = os.environ.get("LXC_MANAGER_PAM_SERVICE", "lxc-manager")
AUTH_IDLE_TIMEOUT = int(os.environ.get("LXC_MANAGER_AUTH_IDLE_SECONDS", str(30 * 60)))
AUTH_MAX_AGE = int(os.environ.get("LXC_MANAGER_AUTH_MAX_AGE_SECONDS", str(8 * 60 * 60)))
AUTH_COOKIE_SECURE = os.environ.get("LXC_MANAGER_AUTH_COOKIE_SECURE", "0") == "1"
AUTH_ALLOWED_USERS = frozenset(filter(None, (value.strip() for value in os.environ.get("LXC_MANAGER_ALLOWED_USERS", "").split(","))))
AUTH_ALLOWED_GROUPS = frozenset(filter(None, (value.strip() for value in os.environ.get("LXC_MANAGER_ALLOWED_GROUPS", "").split(","))))
AUTH_USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{1,128}$")
AUTH_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
AUTH_PUBLIC_PATHS = frozenset({"/login", "/auth/login", "/auth/status", "/health"})
SHELL_IDLE_TIMEOUT = 30 * 60
SHELL_RECONNECT_GRACE = 60
DEFAULT_SHELL = "/bin/sh"
SHELL_CANDIDATES = ("/bin/sh", "/bin/bash", "/bin/fish", "/bin/zsh", "/usr/bin/bash", "/usr/bin/fish", "/usr/bin/zsh")
PREFERRED_SHELLS = ("/bin/bash", "/bin/zsh", "/bin/fish", "/bin/sh")
SHELL_PATH_RE = re.compile(r"^/[A-Za-z0-9._+@%/-]+$")
HOP_BY_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}


class ShellSession:
    def __init__(self, session_id: str, name: str, shell: str, process, username: str):
        self.session_id = session_id
        self.name = name
        self.shell = shell
        self.process = process
        self.username = username
        self.output: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=256)
        self.reader_task: Optional[asyncio.Task] = None
        self.grace_task: Optional[asyncio.Task] = None
        self.websocket: Optional[WebSocket] = None
        self.closed = False


SHELL_SESSIONS: dict[str, ShellSession] = {}


@dataclass(frozen=True)
class Principal:  # noqa: F811  (re-export alias; domain.Principal is the canonical one)
    username: str
    uid: int
    gid: int
    home: str
    shell: str


@dataclass
class SessionRecord:  # noqa: F811
    principal: Principal
    csrf_hash: str
    created_at: float
    last_seen: float
    expires_at: float


@dataclass
class LoginThrottle:  # noqa: F811
    failures: int = 0
    retry_after: float = 0.0


class LinuxAuthenticator:
    """Authenticate a real NSS user through the host's configured PAM stack."""

    def __init__(self, service: str = PAM_SERVICE):
        self.service = service

    @staticmethod
    def _login_shell(shell: str) -> bool:
        if not shell or not Path(shell).is_file() or not os.access(shell, os.X_OK):
            return False
        try:
            allowed = {line.strip() for line in Path("/etc/shells").read_text().splitlines() if line.strip() and not line.startswith("#")}
        except OSError:
            allowed = set()
        return not allowed or shell in allowed

    def authenticate(self, username: str, password: str) -> Optional[Principal]:
        if not AUTH_USERNAME_RE.fullmatch(username):
            return None
        try:
            account = pwd.getpwnam(username)
        except KeyError:
            return None
        if not self._login_shell(account.pw_shell):
            return None
        try:
            import pam
            authenticated = pam.pam().authenticate(username, password, service=self.service)
        except Exception as error:
            logger.error("PAM authentication is unavailable: %s", error)
            return None
        if not authenticated:
            return None
        return Principal(username, account.pw_uid, account.pw_gid, account.pw_dir, account.pw_shell)


AUTHENTICATOR = LinuxAuthenticator()
AUTH_SESSIONS: dict[str, SessionRecord] = {}
LOGIN_THROTTLES: dict[str, LoginThrottle] = {}
AUTH_STATE_LOCK = threading.Lock()


def is_loopback(host: Optional[str]) -> bool:
    try:
        return host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def user_is_authorized(principal: Principal) -> bool:
    if AUTH_ALLOWED_USERS and principal.username not in AUTH_ALLOWED_USERS:
        return False
    if not AUTH_ALLOWED_GROUPS:
        return True
    try:
        groups = {group.gr_name for group in grp.getgrall() if principal.username in group.gr_mem}
        groups.update(group.gr_name for group in grp.getgrall() if group.gr_gid == principal.gid)
        return bool(groups & AUTH_ALLOWED_GROUPS)
    except OSError:
        return False


def _prune_auth_state(now: Optional[float] = None) -> None:
    now = time.monotonic() if now is None else now
    with AUTH_STATE_LOCK:
        for token, session in list(AUTH_SESSIONS.items()):
            if session.expires_at <= now or session.last_seen + AUTH_IDLE_TIMEOUT <= now:
                AUTH_SESSIONS.pop(token, None)
        for key, throttle in list(LOGIN_THROTTLES.items()):
            if throttle.failures == 0 and throttle.retry_after <= now:
                LOGIN_THROTTLES.pop(key, None)


def _session_principal(token: Optional[str]) -> Optional[Principal]:
    if not token:
        return None
    now = time.monotonic()
    _prune_auth_state(now)
    token_hash = _token_hash(token)
    with AUTH_STATE_LOCK:
        session = AUTH_SESSIONS.get(token_hash)
        if not session or session.expires_at <= now or session.last_seen + AUTH_IDLE_TIMEOUT <= now:
            AUTH_SESSIONS.pop(token_hash, None)
            return None
        session.last_seen = now
        return session.principal


def _session_record(token: Optional[str]) -> Optional[SessionRecord]:
    if not token:
        return None
    _prune_auth_state()
    with AUTH_STATE_LOCK:
        return AUTH_SESSIONS.get(_token_hash(token))


def _create_session(principal: Principal) -> tuple[str, str]:
    now = time.monotonic()
    session_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    with AUTH_STATE_LOCK:
        AUTH_SESSIONS[_token_hash(session_token)] = SessionRecord(
            principal=principal,
            csrf_hash=_token_hash(csrf_token),
            created_at=now,
            last_seen=now,
            expires_at=now + AUTH_MAX_AGE,
        )
    return session_token, csrf_token


def _revoke_session(token: Optional[str]) -> None:
    if token:
        with AUTH_STATE_LOCK:
            AUTH_SESSIONS.pop(_token_hash(token), None)


def _throttle_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}:{username.casefold()}"


def _login_allowed(key: str) -> bool:
    now = time.monotonic()
    with AUTH_STATE_LOCK:
        throttle = LOGIN_THROTTLES.get(key)
        return not throttle or throttle.retry_after <= now


def _record_login_failure(key: str) -> None:
    now = time.monotonic()
    with AUTH_STATE_LOCK:
        throttle = LOGIN_THROTTLES.setdefault(key, LoginThrottle())
        throttle.failures += 1
        throttle.retry_after = now + min(60.0, 2 ** min(throttle.failures, 6))


def _clear_login_failures(key: str) -> None:
    with AUTH_STATE_LOCK:
        LOGIN_THROTTLES.pop(key, None)


def _current_principal(request: Request) -> Optional[Principal]:
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        return principal
    principal = _session_principal(request.cookies.get(AUTH_COOKIE))
    if principal is not None:
        request.state.principal = principal
    return principal


def require_authenticated_user(request: Request) -> Principal:
    principal = _current_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication is required.", headers={"WWW-Authenticate": "Session"})
    if not user_is_authorized(principal):
        raise HTTPException(status_code=403, detail="This Linux account is not authorized to use LXC Manager.")
    return principal


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    parsed = urlsplit(origin)
    return parsed.scheme == request.url.scheme and parsed.netloc == request.headers.get("host")


def _is_container_proxy_path(path: str) -> bool:
    return path.startswith("/containers/") and "/web/" in path


def _validate_csrf(request: Request, session: SessionRecord) -> None:
    if request.method not in AUTH_STATE_CHANGING_METHODS:
        return
    if _is_container_proxy_path(request.url.path):
        if not _same_origin(request):
            raise HTTPException(status_code=403, detail="A same-origin request is required.")
        return
    csrf_token = request.headers.get("x-csrf-token")
    if not csrf_token or not hmac.compare_digest(_token_hash(csrf_token), session.csrf_hash):
        raise HTTPException(status_code=403, detail="CSRF validation failed.")
    if request.headers.get("origin") and not _same_origin(request):
        raise HTTPException(status_code=403, detail="A same-origin request is required.")


def _websocket_principal(websocket: WebSocket) -> Optional[Principal]:
    if not is_loopback(websocket.client.host if websocket.client else None):
        return None
    origin = websocket.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        expected_scheme = "https" if websocket.url.scheme == "wss" else "http"
        if parsed.netloc != websocket.headers.get("host") or parsed.scheme != expected_scheme:
            return None
    return _session_principal(websocket.cookies.get(AUTH_COOKIE))


def require_local_access(request: Request) -> None:
    if not is_loopback(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="This feature is available from localhost only.")
    require_authenticated_user(request)


def require_websocket_access(websocket: WebSocket) -> bool:
    principal = _websocket_principal(websocket)
    return principal is not None and user_is_authorized(principal)


async def run_cmd(cmd: List[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run a noninteractive command without access to the server's terminal."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip()
    except asyncio.TimeoutError:
        return -1, "", f"Command timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"
    except Exception as e:
        return -1, "", str(e)


async def run_lxc_cmd(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run an lxc-* command."""
    return await run_cmd([f"{LXC_PATH}/lxc-attach"] + list(args), timeout) if args[0] == "attach" else await run_cmd([f"{LXC_PATH}/lxc-{args[0]}"] + list(args[1:]), timeout)


async def run_lxc_attach(name: str, *cmd_args: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a command inside a container via lxc-attach."""
    return await run_cmd(
        [f"{LXC_PATH}/lxc-attach", "-n", name, "--"] + list(cmd_args),
        timeout=timeout,
    )


def parse_lxc_ls_output(output: str) -> List[ContainerResponse]:
    """Parse lxc-ls -f -F NAME,STATE,PID,IPV4,IPV6 output."""
    containers = []
    lines = output.strip().split('\n')
    if len(lines) < 2:
        return containers

    columns = [(match.group(), match.start()) for match in re.finditer(r'\S+', lines[0])]
    if not {"NAME", "STATE", "PID", "IPV4", "IPV6"}.issubset(key for key, _ in columns):
        return containers
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = {
            key: line[start:columns[index + 1][1] if index + 1 < len(columns) else len(line)].strip()
            for index, (key, start) in enumerate(columns)
        }
        if fields["NAME"] and fields["STATE"]:
            containers.append(ContainerResponse(
                name=fields["NAME"],
                status=fields["STATE"],
                pid=int(fields["PID"]) if fields["PID"].isdigit() else None,
                ipv4=fields["IPV4"] if fields["IPV4"] not in ('-', '') else None,
                ipv6=fields["IPV6"] if fields["IPV6"] not in ('-', '') else None,
            ))
    return containers


async def get_container_list() -> List[ContainerResponse]:
    """Get list of all containers with their status."""
    code, stdout, stderr = await run_lxc_cmd("ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6")
    if code != 0:
        raise HTTPException(status_code=500, detail=f"Failed to list containers: {stderr}")
    return parse_lxc_ls_output(stdout)


async def get_container_info(name: str) -> Optional[ContainerResponse]:
    """Get detailed info for a specific container."""
    containers = await get_container_list()
    for c in containers:
        if c.name == name:
            return c
    return None


async def get_container_ipv4(name: str) -> str:
    """Read a container's primary IPv4 address from its own network namespace."""
    code, stdout, stderr = await run_lxc_attach(
        name, "sh", "-c", "ip -4 -o addr show scope global | awk '$2 != \"lo\" {sub(/\\/.*/, \"\", $4); print $4; exit}'", timeout=10,
    )
    if code != 0:
        raise HTTPException(status_code=502, detail=f"Unable to determine the container address: {stderr}")
    try:
        address = ipaddress.ip_address(stdout.strip())
    except ValueError as error:
        raise HTTPException(status_code=502, detail="The container has no usable IPv4 address. Check its network setup.") from error
    if not address.is_private:
        raise HTTPException(status_code=502, detail="The container address is not a private IPv4 address.")
    return str(address)


def proxy_headers(headers) -> dict:
    return {key: value for key, value in headers.items() if key.lower() not in HOP_BY_HOP_HEADERS | {"host", "cookie"}}


def rewrite_proxy_location(location: str, base_path: str, upstream: str) -> str:
    if location.startswith(upstream):
        return base_path + location[len(upstream):]
    if location.startswith("/"):
        return base_path + location
    return location


async def get_container_metrics(name: str, include_snapshots: bool = True) -> ContainerMetrics:
    """Get resource metrics for a running container."""
    metrics = ContainerMetrics(name=name)

    started = time.monotonic()
    code, stdout, _ = await run_lxc_attach(name, "cat", "/sys/fs/cgroup/cpu.stat", timeout=5)
    if code == 0 and stdout:
        try:
            first = int(dict(line.split() for line in stdout.splitlines())["usage_usec"])
            await asyncio.sleep(0.2)
            code, sample, _ = await run_lxc_attach(name, "cat", "/sys/fs/cgroup/cpu.stat", timeout=5)
            elapsed = time.monotonic() - started
            if code == 0:
                second = int(dict(line.split() for line in sample.splitlines())["usage_usec"])
                metrics.cpu_usage_percent = max(0.0, (second - first) / (elapsed * 10000))
        except (ValueError, IndexError, KeyError):
            pass

    code, stdout, _ = await run_lxc_attach(name, "cat", "/sys/fs/cgroup/memory.current", timeout=5)
    if code == 0 and stdout:
        try:
            metrics.memory_usage_bytes = int(stdout.strip())
        except ValueError:
            pass

    code, stdout, _ = await run_lxc_attach(name, "cat", "/sys/fs/cgroup/memory.max", timeout=5)
    if code == 0 and stdout and stdout.strip() != "max":
        try:
            metrics.memory_limit_bytes = int(stdout.strip())
        except ValueError:
            pass

    code, stdout, _ = await run_lxc_attach(name, "cat", "/sys/fs/cgroup/io.stat", timeout=5)
    if code == 0 and stdout:
        totals = {"rbytes": 0, "wbytes": 0}
        for line in stdout.splitlines():
            for field in line.split()[1:]:
                key, _, value = field.partition("=")
                if key in totals:
                    try:
                        totals[key] += int(value)
                    except ValueError:
                        pass
        metrics.disk_read_bytes = totals["rbytes"]
        metrics.disk_write_bytes = totals["wbytes"]

    code, stdout, _ = await run_lxc_attach(name, "cat", "/proc/net/dev", timeout=5)
    if code == 0 and stdout:
        for line in stdout.splitlines()[2:]:
            parts = line.split()
            if len(parts) >= 10 and parts[0].rstrip(":") != "lo":
                try:
                    metrics.network_rx_bytes = (metrics.network_rx_bytes or 0) + int(parts[1])
                    metrics.network_tx_bytes = (metrics.network_tx_bytes or 0) + int(parts[9])
                except (ValueError, IndexError):
                    pass

    if include_snapshots:
        metrics.snapshots = await get_snapshot_list(name)
    return metrics


async def get_snapshot_list(name: str) -> List[str]:
    snapshots = []
    code, stdout, stderr = await run_lxc_cmd("snapshot", "-n", name, "-L")
    if code != 0:
        raise HTTPException(status_code=500, detail=f"Failed to list snapshots: {stderr}")
    if code == 0 and stdout:
        for line in stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("Snapshots"):
                snap_name = line.split()[0]
                if snap_name:
                    snapshots.append(snap_name)
    return snapshots


# NOTE: lifespan and /static mount are defined in lxc_manager.app; this app
# already carries them.


@app.middleware("http")
async def authentication_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static/") or path in AUTH_PUBLIC_PATHS:
        return await call_next(request)
    principal = _current_principal(request)
    if principal is None:
        if path == "/":
            next_path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(next_path, safe='')}", status_code=303)
        return JSONResponse(
            {"detail": "Authentication is required."},
            status_code=401,
            headers={"WWW-Authenticate": "Session"},
        )
    if not user_is_authorized(principal):
        return JSONResponse({"detail": "This Linux account is not authorized to use LXC Manager."}, status_code=403)
    request.state.principal = principal
    session = _session_record(request.cookies.get(AUTH_COOKIE))
    if session is None:
        return JSONResponse({"detail": "Authentication is required."}, status_code=401)
    try:
        _validate_csrf(request, session)
    except HTTPException as error:
        return JSONResponse({"detail": error.detail}, status_code=error.status_code)
    return await call_next(request)


@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-store"})


@app.get("/auth/status")
async def auth_status(request: Request):
    principal = _current_principal(request)
    return {
        "authenticated": principal is not None and user_is_authorized(principal),
        "username": principal.username if principal and user_is_authorized(principal) else None,
    }


@app.post("/auth/login", status_code=204)
async def login(request: Request, credentials: LoginCredentials):
    if not is_loopback(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="Login is available from localhost only.")
    username = credentials.username.strip()
    throttle_key = _throttle_key(request, username)
    if not _login_allowed(throttle_key):
        raise HTTPException(status_code=429, detail="Login temporarily unavailable. Try again shortly.")
    principal = await asyncio.to_thread(AUTHENTICATOR.authenticate, username, credentials.password)
    if principal is None or not user_is_authorized(principal):
        _record_login_failure(throttle_key)
        raise HTTPException(status_code=401, detail="Invalid Linux username or password.")
    _clear_login_failures(throttle_key)
    session_token, csrf_token = _create_session(principal)
    response = Response(status_code=204)
    cookie_options = {
        "path": "/",
        "httponly": True,
        "secure": AUTH_COOKIE_SECURE,
        "samesite": "strict",
    }
    response.set_cookie(AUTH_COOKIE, session_token, max_age=AUTH_MAX_AGE, **cookie_options)
    response.set_cookie(CSRF_COOKIE, csrf_token, max_age=AUTH_MAX_AGE, httponly=False, secure=AUTH_COOKIE_SECURE, samesite="strict", path="/")
    return response


@app.post("/auth/logout", status_code=204)
async def logout(request: Request):
    require_authenticated_user(request)
    _revoke_session(request.cookies.get(AUTH_COOKIE))
    response = Response(status_code=204)
    response.delete_cookie(AUTH_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    code, _, stderr = await run_lxc_cmd("ls", "-f", "-F", "NAME")
    return {
        "status": "healthy" if code == 0 else "degraded",
        "lxc_accessible": code == 0,
        "error": stderr if code != 0 else None,
    }


def acl_allows_traversal(acl: str, uid: int, gid: int, owner_uid: int, owner_gid: int) -> bool:
    """Evaluate POSIX access ACL for container root (no supplementary groups)."""
    entries = {}
    for line in acl.splitlines():
        parts = line.split("#", 1)[0].strip().split(":")
        if len(parts) == 3 and parts[0] in ("user", "group", "mask", "other"):
            entries[(parts[0], parts[1])] = parts[2].strip()
    if ("user", "") not in entries or ("other", "") not in entries:
        raise ValueError("Incomplete access ACL")
    mask_exec = "x" in entries.get(("mask", ""), "rwx")
    if uid == owner_uid:
        return "x" in entries[("user", "")]
    if ("user", str(uid)) in entries:
        return mask_exec and "x" in entries[("user", str(uid))]
    group_permissions = []
    if gid == owner_gid:
        group_permissions.append(entries.get(("group", ""), "---"))
    if ("group", str(gid)) in entries:
        group_permissions.append(entries[("group", str(gid))])
    if group_permissions:
        return mask_exec and any("x" in permissions for permissions in group_permissions)
    return "x" in entries[("other", "")]


async def check_rootfs_traversal(rootfs: Path, uid: int, gid: int) -> list:
    checks = []
    for directory in reversed(rootfs.resolve().parents):
        try:
            metadata = directory.stat()
            code, acl, error = await run_cmd(["getfacl", "-cpn", "--", str(directory)], timeout=5)
            if code != 0:
                checks.append({"id": "directory_traversal", "status": "unknown", "path": str(directory), "message": f"Unable to inspect ACL: {error}"})
                continue
            accessible = acl_allows_traversal(acl, uid, gid, metadata.st_uid, metadata.st_gid)
            result = {"id": "directory_traversal", "status": "ok" if accessible else "error", "path": str(directory), "message": f"Mapped root UID {uid} {'can' if accessible else 'cannot'} traverse this directory."}
            if not accessible:
                result["remediation"] = f"setfacl -m u:{uid}:--x -- {shlex.quote(str(directory))}"
                result["remediation_note"] = "Run in your host terminal. Grants traversal to mapped container root; review existing ACLs before applying."
            checks.append(result)
        except (OSError, ValueError) as error:
            checks.append({"id": "directory_traversal", "status": "unknown", "path": str(directory), "message": str(error)})
    return checks


@app.get("/doctor")
async def doctor(name: Optional[str] = Query(None, min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")):
    """Read-only checks for LXC accessibility, namespaces, and rootfs traversal."""
    checks = []
    code, _, error = await run_lxc_cmd("ls", "-1")
    checks.append({"id": "lxc_access", "status": "ok" if code == 0 else "error", "message": "LXC listing is accessible." if code == 0 else error})
    restricted_namespace = None
    try:
        mapping = Path("/proc/self/uid_map").read_text().split()
        restricted_namespace = mapping != ["0", "0", "4294967295"]
        checks.append({"id": "user_namespace", "status": "warning" if restricted_namespace else "ok", "message": "App is already inside a user namespace; nested LXC may be restricted and host ACL evaluation is unavailable." if restricted_namespace else "App runs in the host user namespace."})
        status = Path("/proc/self/status").read_text()
        no_new_privileges = bool(re.search(r"^NoNewPrivs:\s+1$", status, re.M))
        checks.append({"id": "privilege_helpers", "status": "warning" if no_new_privileges else "ok", "message": "NoNewPrivs is enabled; privileged UID/GID mapping helpers may fail." if no_new_privileges else "NoNewPrivs is disabled."})
    except OSError as error:
        checks.append({"id": "process_restrictions", "status": "unknown", "message": str(error)})

    home = Path.home()
    rootfs = home / ".local/share/lxc" / (name or "new-container") / "rootfs"
    config = home / ".local/share/lxc" / name / "config" if name else home / ".config/lxc/default.conf"
    if name and not config.is_file():
        raise HTTPException(status_code=404, detail=f"Container config for '{name}' not found")
    try:
        text = config.read_text()
        uid_match = re.search(r"^\s*lxc.idmap\s*=\s*u\s+0\s+(\d+)\s+\d+\s*$", text, re.M)
        gid_match = re.search(r"^\s*lxc.idmap\s*=\s*g\s+0\s+(\d+)\s+\d+\s*$", text, re.M)
        rootfs_match = re.search(r"^\s*lxc.rootfs.path\s*=\s*(\S+)\s*$", text, re.M)
        if rootfs_match:
            value = rootfs_match[1]
            if value.startswith("dir:"):
                value = value[4:]
            if not value.startswith("/"):
                raise ValueError("Doctor only supports absolute directory rootfs paths.")
            rootfs = Path(value)
        if not uid_match or not gid_match:
            raise ValueError("Root UID/GID mapping is not declared directly in this config; included configs are not evaluated.")
        uid, gid = int(uid_match[1]), int(gid_match[1])
        checks.append({"id": "root_mapping", "status": "ok", "message": f"Container root maps to host UID {uid}, GID {gid}."})
        if restricted_namespace is False:
            if not name:
                rootfs = home / ".local/share/lxc" / "new-container"
            checks.extend(await check_rootfs_traversal(rootfs, uid, gid))
        else:
            checks.append({"id": "directory_traversal", "status": "unknown", "message": "Run the app from the host terminal to evaluate host UID permissions."})
    except (OSError, ValueError) as error:
        checks.append({"id": "root_mapping", "status": "unknown", "message": str(error)})
    overall = "error" if any(c["status"] == "error" for c in checks) else "warning" if any(c["status"] in ("warning", "unknown") for c in checks) else "ok"
    return {"status": overall, "container": name, "read_only": True, "checks": checks, "scope": "Checks do not prove container startup, networking, or image availability."}


@app.get("/images")
async def list_available_images():
    """List available base image distributions."""
    return {
        "images": [
            {
                "distro": distro,
                "default_release": info["release"],
                "alias": info["alias"],
            }
            for distro, info in sorted(DISTRO_IMAGES.items())
        ]
    }


def download_image_index() -> str:
    """Match lxc-download's system image index and compatibility fallback."""
    base = "https://images.linuxcontainers.org/meta/1.0/index-system"
    for url in (base + ".7", base):
        try:
            request = UrlRequest(url, headers={"User-Agent": "lxc/7.0.0 compat:7"})
            with urlopen(request, timeout=12) as response:
                return response.read().decode("utf-8")
        except HTTPError as error:
            if error.code == 404 and url.endswith(".7"):
                continue
            raise
    raise RuntimeError("Image catalog unavailable")


@app.get("/images/{distro}/releases")
async def list_image_releases(distro: str, arch: str = Query("amd64", pattern=r"^(amd64|arm64|i386)$")):
    distro = distro.lower()
    if distro not in DISTRO_IMAGES:
        raise HTTPException(status_code=400, detail="Unsupported distribution")
    try:
        index = await asyncio.to_thread(download_image_index)
    except (URLError, TimeoutError, OSError) as error:
        raise HTTPException(status_code=502, detail="Unable to fetch the LXC image catalog. Please retry.") from error
    releases = set()
    for line in index.splitlines():
        fields = line.strip().split(";")
        if len(fields) >= 6 and fields[0] == distro and fields[2] == arch and fields[3] == "default":
            releases.add(fields[1])
    return {"distro": distro, "arch": arch, "releases": sorted(releases), "default_release": DISTRO_IMAGES[distro]["release"]}


def validate_shell_path(shell: str) -> str:
    if not isinstance(shell, str) or len(shell) > 256 or not SHELL_PATH_RE.fullmatch(shell):
        raise ValueError("Shell must be an absolute executable path without arguments.")
    return shell


async def shell_available(name: str, shell: str) -> bool:
    code, _, _ = await run_lxc_attach(name, "test", "-x", shell, timeout=5)
    return code == 0


async def available_shells(name: str) -> List[str]:
    checks = await asyncio.gather(*(shell_available(name, shell) for shell in SHELL_CANDIDATES))
    return [shell for shell, available in zip(SHELL_CANDIDATES, checks) if available]


def default_shell(shells: List[str]) -> str:
    return next((shell for shell in PREFERRED_SHELLS if shell in shells), DEFAULT_SHELL)


async def enqueue_shell_output(session: ShellSession, output: Optional[bytes]) -> None:
    if session.output.full():
        with suppress(asyncio.QueueEmpty):
            session.output.get_nowait()
    with suppress(asyncio.QueueFull):
        session.output.put_nowait(output)


async def read_shell_output(session: ShellSession) -> None:
    try:
        while True:
            output = await session.process.stdout.read(4096)
            if not output:
                break
            await enqueue_shell_output(session, output)
    finally:
        session.closed = True
        await enqueue_shell_output(session, None)
        if session.websocket is None:
            SHELL_SESSIONS.pop(session.session_id, None)


async def terminate_shell_session(session: ShellSession) -> None:
    if session.closed and session.process.returncode is not None:
        SHELL_SESSIONS.pop(session.session_id, None)
        return
    session.closed = True
    if session.grace_task and session.grace_task is not asyncio.current_task():
        session.grace_task.cancel()
    if session.process.returncode is None:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(session.process.pid, signal.SIGTERM)
        with suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(session.process.wait(), timeout=3)
    if session.process.returncode is None:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(session.process.pid, signal.SIGKILL)
        with suppress(Exception):
            await session.process.wait()
    if session.reader_task and session.reader_task is not asyncio.current_task():
        session.reader_task.cancel()
        await asyncio.gather(session.reader_task, return_exceptions=True)
    SHELL_SESSIONS.pop(session.session_id, None)


async def expire_shell_session(session: ShellSession) -> None:
    try:
        await asyncio.sleep(SHELL_RECONNECT_GRACE)
        if session.websocket is None and not session.closed:
            await terminate_shell_session(session)
    except asyncio.CancelledError:
        pass


async def create_shell_session(name: str, shell: str, session_id: str, username: str) -> ShellSession:
    shell_environment = {
        **os.environ,
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "SHELL": shell,
        "PS1": f"{name}# ",
    }
    process = await asyncio.create_subprocess_exec(
        f"{LXC_PATH}/lxc-attach", "-n", name, "--",
        "script", "-qefc", f"{shell} -i", "/dev/null",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=shell_environment, start_new_session=True,
    )
    session = ShellSession(session_id, name, shell, process, username)
    session.reader_task = asyncio.create_task(read_shell_output(session))
    SHELL_SESSIONS[session_id] = session
    return session


@app.get("/containers/{name}/shells")
async def list_shells(request: Request, name: str):
    require_local_access(request)
    container = await get_container_info(name)
    if not container or container.status != "RUNNING":
        raise HTTPException(status_code=409, detail="The container must be running.")
    shells = await available_shells(name)
    return {"shells": shells, "default": default_shell(shells)}


@app.websocket("/containers/{name}/shell")
async def container_shell(websocket: WebSocket, name: str, shell: str = DEFAULT_SHELL, session_id: Optional[str] = None):
    """Attach a reconnectable browser session to an interactive shell."""
    if not require_websocket_access(websocket):
        await websocket.close(code=1008, reason="Authentication and localhost access are required.")
        return
    principal = _websocket_principal(websocket)
    if principal is None:
        await websocket.close(code=1008, reason="Authentication and localhost access are required.")
        return
    container = await get_container_info(name)
    if not container or container.status != "RUNNING":
        await websocket.close(code=1008, reason="The container must be running.")
        return
    try:
        shell = validate_shell_path(shell)
    except ValueError as error:
        await websocket.close(code=1008, reason=str(error))
        return
    session_id = session_id or str(uuid4())
    session = SHELL_SESSIONS.get(session_id)
    if session and (session.name != name or session.shell != shell or session.username != principal.username or session.closed):
        await websocket.close(code=1008, reason="Session identity does not match the requested shell.")
        return
    if session is None:
        if not await shell_available(name, shell):
            await websocket.close(code=1008, reason=f"Shell is unavailable inside {name}: {shell}")
            return
        session = await create_shell_session(name, shell, session_id, principal.username)
    await websocket.accept()
    if session.websocket and session.websocket is not websocket:
        with suppress(RuntimeError, OSError):
            await session.websocket.close(code=4001, reason="Session reconnected elsewhere")
    session.websocket = websocket
    if session.grace_task:
        session.grace_task.cancel()
        session.grace_task = None

    async def send_input(data: str):
        if session.process.stdin is None or session.process.returncode is not None:
            return
        session.process.stdin.write(data.replace("\r\n", "\n").replace("\r", "\n").encode())
        await session.process.stdin.drain()

    async def forward_output():
        while True:
            output = await session.output.get()
            if output is None:
                break
            await websocket.send_bytes(output)

    async def forward_input():
        while True:
            message = await asyncio.wait_for(websocket.receive_json(), timeout=SHELL_IDLE_TIMEOUT)
            kind = message.get("type")
            if kind == "input" and isinstance(message.get("data"), str):
                await send_input(message["data"])
            elif kind == "close":
                return True

    output_task = asyncio.create_task(forward_output())
    input_task = asyncio.create_task(forward_input())
    close_reason = "Shell session ended"
    terminate = False
    try:
        done, _ = await asyncio.wait({output_task, input_task}, return_when=asyncio.FIRST_COMPLETED)
        if input_task in done:
            if input_task.exception() is None and input_task.result() is True:
                terminate = True
            elif isinstance(input_task.exception(), (asyncio.TimeoutError, TimeoutError)):
                close_reason = "Idle timeout"
        if output_task in done:
            terminate = True
    except WebSocketDisconnect:
        pass
    finally:
        output_task.cancel(); input_task.cancel()
        await asyncio.gather(output_task, input_task, return_exceptions=True)
        if session.websocket is websocket:
            session.websocket = None
        if terminate or session.closed:
            await terminate_shell_session(session)
        elif not session.grace_task:
            session.grace_task = asyncio.create_task(expire_shell_session(session))
        with suppress(RuntimeError, OSError):
            await websocket.close(code=1000, reason=close_reason)


@app.api_route("/containers/{name}/web/{port}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
@app.api_route("/containers/{name}/web/{port}/{proxy_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def proxy_container_web_app(request: Request, name: str, port: int, proxy_path: str = ""):
    """Authenticated local reverse proxy to an HTTP service in a running container."""
    require_local_access(request)
    if not 1 <= port <= 65535:
        raise HTTPException(status_code=422, detail="Port must be between 1 and 65535.")
    container = await get_container_info(name)
    if not container or container.status != "RUNNING":
        raise HTTPException(status_code=409, detail="The container must be running.")
    address = await get_container_ipv4(name)
    upstream = f"http://{address}:{port}"
    base_path = f"/containers/{name}/web/{port}"
    target = f"{upstream}/{proxy_path.lstrip('/')}"
    if request.url.query:
        target += f"?{request.url.query}"
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            upstream_response = await client.request(request.method, target, headers=proxy_headers(request.headers), content=await request.body())
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"Unable to reach the container web app at {address}:{port}: {error}") from error
    response_headers = proxy_headers(upstream_response.headers)
    response_headers.pop("content-encoding", None)
    response_headers.pop("content-length", None)
    response_headers.pop("set-cookie", None)
    if location := upstream_response.headers.get("location"):
        response_headers["location"] = rewrite_proxy_location(location, base_path, upstream)
    response = Response(content=upstream_response.content, status_code=upstream_response.status_code, headers=response_headers)
    for cookie in upstream_response.headers.get_list("set-cookie"):
        response.headers.append("set-cookie", re.sub(r"(?i)(;\s*path=)/", rf"\1{base_path}/", cookie))
    return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8083)
