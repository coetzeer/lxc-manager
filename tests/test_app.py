"""Isolated contract tests: never create or delete real containers.

Container lifecycle tests now inject a fake :class:`lxc_manager.services.lxc.LxcClient`
through FastAPI dependency injection instead of patching ``main.run_lxc_cmd``.
Auth, shell, proxy, doctor, and image tests continue to exercise the
``main.py`` shim functions directly.
"""
import unittest
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from unittest.mock import AsyncMock, patch

import httpx
import main

from lxc_manager.api.deps import get_container_service
from lxc_manager.config import LxcConfig
from lxc_manager.services.containers import ContainerService
from lxc_manager.services.lxc import LxcResult


class FakeLxcClient:
    """In-memory LxcClient that records calls and returns queued results.

    Use :meth:`queue_result` to pre-program the result returned for a
    particular ``(method, *args)`` call. Results are consumed in FIFO order;
    once a queue is exhausted a default successful empty result is returned.
    """

    def __init__(self):
        self.calls = []  # list of (method, args_tuple, kwargs_dict)
        self._queue = {}  # (method, args) -> [LxcResult, ...]

    def queue_result(self, method, *args, result):
        key = (method, args)
        self._queue.setdefault(key, []).append(result)

    async def run(self, *args, timeout=30):
        self.calls.append(("run", args, {"timeout": timeout}))
        return LxcResult(0, "", "")

    async def run_lxc(self, subcommand, *args, timeout=30):
        self.calls.append(("run_lxc", (subcommand,) + args, {"timeout": timeout}))
        key = ("run_lxc", (subcommand,) + args)
        results = self._queue.get(key, [])
        if results:
            return results.pop(0)
        return LxcResult(0, "", "")

    async def run_attach(self, name, *args, timeout=30):
        self.calls.append(("run_attach", (name,) + args, {"timeout": timeout}))
        key = ("run_attach", (name,) + args)
        results = self._queue.get(key, [])
        if results:
            return results.pop(0)
        return LxcResult(0, "", "")


def _ls_output(name: str, status: str, pid: str = "-") -> str:
    return f"NAME STATE   PID IPV4     IPV6\n{name} {status}  {pid}  -    -    "


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app, client=("127.0.0.1", 50000)),
            base_url="http://test",
        )
        class FakeAuthenticator:
            def authenticate(self, username, password):
                if username == "raymond" and password == "correct-password":
                    return main.Principal("raymond", os.getuid(), os.getgid(), "/home/raymond", "/bin/sh")
                return None
        self.authenticator_patch = patch.object(main, "AUTHENTICATOR", FakeAuthenticator())
        self.authenticator_patch.start()
        main.AUTH_SESSIONS.clear()
        main.LOGIN_THROTTLES.clear()
        response = await self.client.post("/auth/login", json={"username": "raymond", "password": "correct-password"})
        self.assertEqual(response.status_code, 204)
        self.config = LxcConfig()

    async def asyncTearDown(self):
        await self.client.aclose()
        self.authenticator_patch.stop()
        main.app.dependency_overrides.clear()

    def _install_service(self, fake_lxc: FakeLxcClient) -> ContainerService:
        service = ContainerService(fake_lxc, self.config)
        main.app.dependency_overrides[get_container_service] = lambda: service
        return service

    def csrf_headers(self):
        return {"X-CSRF-Token": self.client.cookies.get(main.CSRF_COOKIE)}

    # -- untouched: subprocess behaviour -----------------------------------

    async def test_subprocess_has_no_input_or_controlling_terminal(self):
        script = """
import json, os, sys
try:
    fd = os.open('/dev/tty', os.O_RDWR)
except OSError:
    controlling_terminal = False
else:
    os.close(fd)
    controlling_terminal = True
print(json.dumps({'input': sys.stdin.read(), 'terminal': controlling_terminal,
                  'pid': os.getpid(), 'session': os.getsid(0)}))
print('captured stderr', file=sys.stderr)
"""
        code, stdout, stderr = await main.run_cmd([sys.executable, "-c", script], timeout=5)
        self.assertEqual(code, 0)
        data = json.loads(stdout)
        self.assertEqual(data["input"], "")
        self.assertFalse(data["terminal"])
        self.assertEqual(data["session"], data["pid"])
        self.assertNotEqual(data["session"], os.getsid(0))
        self.assertEqual(stderr, "captured stderr")

    async def test_dashboard_and_assets(self):
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn('id="create-form"', response.text)
        self.assertIn('app.js?v=terminal-workspace-4-auth', response.text)
        self.assertIn('/static/lxc-mark.svg', response.text)
        self.assertIn('id="shell-terminal"', response.text)
        self.assertNotIn('id="shell-input"', response.text)
        self.assertNotIn('id="access-form"', response.text)
        for asset, content_type in [("app.js", "javascript"), ("style.css", "text/css"), ("lxc-mark.svg", "image/svg+xml"), ("vendor/xterm/xterm.js", "javascript"), ("vendor/xterm/xterm.js.map", "application/octet-stream"), ("vendor/xterm/addon-fit.js", "javascript"), ("vendor/xterm/addon-fit.js.map", "application/octet-stream"), ("vendor/xterm/xterm.css", "text/css")]:
            response = await self.client.get(f"/static/{asset}")
            self.assertEqual(response.status_code, 200)
            self.assertIn(content_type, response.headers["content-type"])

    # -- auth (unchanged) ---------------------------------------------------

    async def test_local_interactive_access_requires_no_code(self):
        with patch.object(main, "get_container_info", AsyncMock(return_value=None)):
            response = await self.client.get("/containers/demo/web/8080/")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("/auth/login", response.text)

    async def test_login_requires_a_valid_linux_account_and_session(self):
        self.assertEqual((await self.client.get("/auth/status")).json(), {"authenticated": True, "username": "raymond"})
        await self.client.post("/auth/logout", headers=self.csrf_headers())
        self.assertEqual((await self.client.get("/auth/status")).json(), {"authenticated": False, "username": None})
        self.assertEqual((await self.client.post("/auth/login", json={"username": "unknown", "password": "wrong"})).status_code, 401)
        self.assertEqual((await self.client.post("/auth/login", json={"username": "raymond", "password": "correct-password"})).status_code, 204)

    async def test_unauthenticated_dashboard_redirects_to_login(self):
        await self.client.post("/auth/logout", headers=self.csrf_headers())
        response = await self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("/login?next=%2F", response.headers["location"])
        self.assertEqual((await self.client.post("/auth/login", json={"username": "raymond", "password": "correct-password"})).status_code, 204)

    async def test_login_rejects_non_loopback_clients(self):
        remote = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, client=("192.0.2.10", 50000)), base_url="http://test")
        try:
            response = await remote.post("/auth/login", json={"username": "raymond", "password": "correct-password"})
            self.assertEqual(response.status_code, 403)
        finally:
            await remote.aclose()

    async def test_authenticated_mutations_require_csrf_token(self):
        # CSRF is rejected by middleware before the container service runs.
        response = await self.client.post("/containers", json={"name": "demo", "distro": "alpine"})
        self.assertEqual(response.status_code, 403)

    # -- container lifecycle (DI via FakeLxcClient) --------------------------

    async def test_list_and_health(self):
        output = "NAME STATE   PID IPV4     IPV6\ndemo RUNNING 42  10.0.0.2 -"
        fake_lxc = FakeLxcClient()
        fake_lxc.queue_result(
            "run_lxc", "ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6",
            result=LxcResult(0, output, ""),
        )
        self._install_service(fake_lxc)

        response = await self.client.get("/containers")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["name"], "demo")

        # /health still uses main.run_lxc_cmd directly.
        with patch.object(main, "run_lxc_cmd", AsyncMock(return_value=(0, "", ""))):
            self.assertTrue((await self.client.get("/health")).json()["lxc_accessible"])

    async def test_create_payload_and_command(self):
        fake_lxc = FakeLxcClient()
        # _exists -> get -> list: no containers yet
        fake_lxc.queue_result(
            "run_lxc", "ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6",
            result=LxcResult(0, "NAME STATE   PID IPV4     IPV6\n", ""),
        )
        # create
        fake_lxc.queue_result(
            "run_lxc", "create", "-n", "demo", "-t", "download", "--",
            "--dist", "alpine", "--release", "3.23", "--arch", "arm64",
            result=LxcResult(0, "", ""),
        )
        # get after create -> list returns demo STOPPED
        fake_lxc.queue_result(
            "run_lxc", "ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6",
            result=LxcResult(0, _ls_output("demo", "STOPPED"), ""),
        )
        self._install_service(fake_lxc)

        response = await self.client.post(
            "/containers",
            json={"name": "demo", "distro": "alpine", "release": "3.23", "arch": "arm64"},
            headers=self.csrf_headers(),
        )
        self.assertEqual(response.status_code, 201)

        create_calls = [
            c for c in fake_lxc.calls
            if c[0] == "run_lxc" and c[1][0] == "create"
        ]
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(
            create_calls[0][1],
            ("create", "-n", "demo", "-t", "download", "--", "--dist", "alpine", "--release", "3.23", "--arch", "arm64"),
        )
        self.assertEqual(create_calls[0][2]["timeout"], 180)

    async def test_real_lxc_spacing_lists_container_and_blocks_duplicate(self):
        output = "NAME         STATE   PID IPV4 IPV6 \nagent-hermes STOPPED -   -    -    "
        fake_lxc = FakeLxcClient()
        # GET /containers -> list
        fake_lxc.queue_result(
            "run_lxc", "ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6",
            result=LxcResult(0, output, ""),
        )
        # POST /containers -> _exists -> get -> list (duplicate found)
        fake_lxc.queue_result(
            "run_lxc", "ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6",
            result=LxcResult(0, output, ""),
        )
        self._install_service(fake_lxc)

        response = await self.client.get("/containers")
        self.assertEqual(
            response.json(),
            [{"name": "agent-hermes", "status": "STOPPED", "pid": None, "ipv4": None, "ipv6": None}],
        )

        response = await self.client.post(
            "/containers", json={"name": "agent-hermes", "distro": "alpine"}, headers=self.csrf_headers()
        )
        self.assertEqual(response.status_code, 409)

        lxc_calls = [c for c in fake_lxc.calls if c[0] == "run_lxc"]
        self.assertTrue(all(call[1][0] == "ls" for call in lxc_calls))

    async def test_invalid_name_and_duplicate(self):
        # 422 for invalid name: FastAPI validation happens before the service.
        response = await self.client.post(
            "/containers", json={"name": "../bad", "distro": "alpine"}, headers=self.csrf_headers()
        )
        self.assertEqual(response.status_code, 422)

        # 409 for duplicate: service returns AlreadyExists.
        fake_lxc = FakeLxcClient()
        fake_lxc.queue_result(
            "run_lxc", "ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6",
            result=LxcResult(0, _ls_output("demo", "STOPPED"), ""),
        )
        self._install_service(fake_lxc)
        response = await self.client.post(
            "/containers", json={"name": "demo", "distro": "alpine"}, headers=self.csrf_headers()
        )
        self.assertEqual(response.status_code, 409)

    async def test_dashboard_metrics_skip_snapshot_subprocess(self):
        fake_lxc = FakeLxcClient()
        # get -> list returns demo RUNNING (one entry per metrics request)
        fake_lxc.queue_result(
            "run_lxc", "ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6",
            result=LxcResult(0, _ls_output("demo", "RUNNING"), ""),
        )
        fake_lxc.queue_result(
            "run_lxc", "ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6",
            result=LxcResult(0, _ls_output("demo", "RUNNING"), ""),
        )
        # get_metrics calls run_attach for cgroup files -> empty stdout defaults
        self._install_service(fake_lxc)

        response = await self.client.get("/containers/demo/metrics?include_snapshots=false")
        self.assertEqual(response.status_code, 200)
        snapshot_calls = [
            c for c in fake_lxc.calls
            if c[0] == "run_lxc" and c[1][0] == "snapshot"
        ]
        self.assertEqual(len(snapshot_calls), 0)

        response = await self.client.get("/containers/demo/metrics")
        self.assertEqual(response.status_code, 200)
        snapshot_calls = [
            c for c in fake_lxc.calls
            if c[0] == "run_lxc" and c[1][0] == "snapshot" and "-L" in c[1]
        ]
        self.assertEqual(len(snapshot_calls), 1)

    async def test_stopped_metrics_and_snapshot_listing(self):
        fake_lxc = FakeLxcClient()
        # GET /metrics -> get -> list returns demo STOPPED
        fake_lxc.queue_result(
            "run_lxc", "ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6",
            result=LxcResult(0, _ls_output("demo", "STOPPED"), ""),
        )
        # GET /snapshots -> get -> list returns demo STOPPED (second queue entry)
        fake_lxc.queue_result(
            "run_lxc", "ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6",
            result=LxcResult(0, _ls_output("demo", "STOPPED"), ""),
        )
        fake_lxc.queue_result(
            "run_lxc", "snapshot", "-n", "demo", "-L",
            result=LxcResult(0, "snap0 (/path) 2026:10:12 12:00:00\n", ""),
        )
        self._install_service(fake_lxc)

        response = await self.client.get("/containers/demo/metrics")
        self.assertEqual(response.status_code, 400)

        response = await self.client.get("/containers/demo/snapshots")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"snapshots": ["snap0"]})

    async def test_restart_stops_then_starts(self):
        fake_lxc = FakeLxcClient()
        # get -> list returns demo RUNNING
        fake_lxc.queue_result(
            "run_lxc", "ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6",
            result=LxcResult(0, _ls_output("demo", "RUNNING"), ""),
        )
        # stop -> start -> final get -> list returns demo RUNNING
        fake_lxc.queue_result("run_lxc", "stop", "-n", "demo", result=LxcResult(0, "", ""))
        fake_lxc.queue_result("run_lxc", "start", "-n", "demo", result=LxcResult(0, "", ""))
        fake_lxc.queue_result(
            "run_lxc", "ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6",
            result=LxcResult(0, _ls_output("demo", "RUNNING"), ""),
        )
        self._install_service(fake_lxc)

        with patch.object(main.asyncio, "sleep", AsyncMock()):
            response = await self.client.post("/containers/demo/restart", headers=self.csrf_headers())
        self.assertEqual(response.status_code, 200)

        lxc_calls = [c for c in fake_lxc.calls if c[0] == "run_lxc"]
        subcommands = [call[1][0] for call in lxc_calls]
        self.assertIn("stop", subcommands)
        self.assertIn("start", subcommands)
        stop_idx = subcommands.index("stop")
        start_idx = subcommands.index("start")
        self.assertLess(stop_idx, start_idx)

    async def test_missing_container_and_command_error(self):
        fake_lxc = FakeLxcClient()
        # GET /containers/missing/snapshots -> get -> list empty -> 404
        fake_lxc.queue_result(
            "run_lxc", "ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6",
            result=LxcResult(0, "NAME STATE   PID IPV4     IPV6\n", ""),
        )
        self._install_service(fake_lxc)

        response = await self.client.get("/containers/missing/snapshots")
        self.assertEqual(response.status_code, 404)

        # GET /containers -> list fails -> 500 ; /health -> main.run_lxc_cmd fails
        fake_lxc.queue_result(
            "run_lxc", "ls", "-f", "-F", "NAME,STATE,PID,IPV4,IPV6",
            result=LxcResult(1, "", "LXC unavailable"),
        )
        response = await self.client.get("/containers")
        self.assertEqual(response.status_code, 500)

        with patch.object(main, "run_lxc_cmd", AsyncMock(return_value=(1, "", "LXC unavailable"))):
            self.assertFalse((await self.client.get("/health")).json()["lxc_accessible"])

    # -- untouched: direct function calls ------------------------------------

    def test_parser_preserves_multiple_ips_and_empty_listing(self):
        output = f'{"NAME":<13}{"STATE":<8}{"PID":<6}{"IPV4":<24}IPV6\n'
        output += f'{"demo":<13}{"RUNNING":<8}{"123":<6}{"10.0.0.2, 10.0.0.3":<24}fd00::2'
        container = main.parse_lxc_ls_output(output)[0]
        self.assertEqual(container.ipv4, "10.0.0.2, 10.0.0.3")
        self.assertEqual(container.ipv6, "fd00::2")
        self.assertEqual(container.pid, 123)
        self.assertEqual(main.parse_lxc_ls_output(""), [])
        self.assertEqual(main.parse_lxc_ls_output("NAME STATE PID IPV4 IPV6"), [])

    async def test_metrics_cpu_delta_and_disk_totals(self):
        values = [(0, "usage_usec 1000000", ""), (0, "usage_usec 1250000", ""), (0, "1024", ""), (0, "max", ""), (0, "8:0 rbytes=10 wbytes=20 rios=1\n8:1 rbytes=30 wbytes=40", ""), (0, "header\nheader\neth0: 100 0 0 0 0 0 0 0 200 0 0 0 0 0 0 0", "")]
        with patch.object(main, "run_lxc_attach", AsyncMock(side_effect=values)), patch.object(main, "get_snapshot_list", AsyncMock(return_value=["snap0"])), patch.object(main.time, "monotonic", side_effect=[10.0, 10.5]), patch.object(main.asyncio, "sleep", AsyncMock()):
            metrics = await main.get_container_metrics("demo")
        self.assertEqual(metrics.cpu_usage_percent, 50.0)
        self.assertEqual(metrics.disk_read_bytes, 40)
        self.assertEqual(metrics.disk_write_bytes, 60)
        self.assertEqual(metrics.network_tx_bytes, 200)
        self.assertIsNone(metrics.memory_limit_bytes)

    # -- proxy / images / doctor (unchanged) ---------------------------------

    async def test_proxy_requires_no_code_on_localhost(self):
        with patch.object(main, "get_container_info", AsyncMock(return_value=None)):
            response = await self.client.get("/containers/demo/web/8080/")
        self.assertEqual(response.status_code, 409)

    async def test_shell_path_validation_and_discovery(self):
        self.assertEqual(main.validate_shell_path("/bin/sh"), "/bin/sh")
        for invalid in ("sh", "/bin/sh -i", "/bin/sh\nwhoami"):
            with self.assertRaises(ValueError):
                main.validate_shell_path(invalid)
        with patch.object(main, "require_local_access"), \
                patch.object(main, "get_container_info", AsyncMock(return_value=main.ContainerResponse(name="demo", status="RUNNING"))), \
                patch.object(main, "run_lxc_attach", AsyncMock(return_value=(0, "", ""))):
            response = await self.client.get("/containers/demo/shells")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["default"], "/bin/bash")
        self.assertEqual(response.json()["shells"], list(main.SHELL_CANDIDATES))

    async def test_container_ipv4_and_proxy_helpers(self):
        with patch.object(main, "run_lxc_attach", AsyncMock(return_value=(0, "10.0.3.42", ""))):
            self.assertEqual(await main.get_container_ipv4("demo"), "10.0.3.42")
        with patch.object(main, "run_lxc_attach", AsyncMock(return_value=(0, "", ""))):
            with self.assertRaises(main.HTTPException) as raised:
                await main.get_container_ipv4("demo")
            self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(main.rewrite_proxy_location("http://10.0.3.42:3000/login", "/containers/demo/web/3000", "http://10.0.3.42:3000"), "/containers/demo/web/3000/login")
        self.assertEqual(main.rewrite_proxy_location("/login", "/containers/demo/web/3000", "http://10.0.3.42:3000"), "/containers/demo/web/3000/login")
        self.assertNotIn("cookie", main.proxy_headers({"Cookie": "secret", "X-Test": "ok", "Connection": "close"}))

    async def test_local_proxy_routes_to_private_container_and_rewrites_redirect(self):
        captured = {}

        class FakeUpstreamResponse:
            status_code = 302
            content = b"redirecting"
            headers = httpx.Headers({"location": "/login", "set-cookie": "app=session; Path=/; HttpOnly"})

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def request(self, method, url, **kwargs):
                captured.update(method=method, url=url, **kwargs)
                return FakeUpstreamResponse()

        running = main.ContainerResponse(name="demo", status="RUNNING")
        with patch.object(main, "get_container_info", AsyncMock(return_value=running)), patch.object(main, "get_container_ipv4", AsyncMock(return_value="10.0.3.42")), patch.object(main.httpx, "AsyncClient", return_value=FakeClient()):
            response = await self.client.get("/containers/demo/web/3000/start?theme=dark")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(captured["url"], "http://10.0.3.42:3000/start?theme=dark")
        self.assertEqual(response.headers["location"], "/containers/demo/web/3000/login")
        self.assertIn("Path=/containers/demo/web/3000/", response.headers["set-cookie"])

    async def test_images(self):
        response = await self.client.get("/images")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(i["distro"] == "alpine" for i in response.json()["images"]))

    def test_doctor_acl_owner_named_user_mask_and_groups(self):
        acl = "user::rwx\ngroup::---\nother::---"
        self.assertFalse(main.acl_allows_traversal(acl, 100000, 100000, 1000, 1000))
        self.assertTrue(main.acl_allows_traversal(acl, 1000, 1000, 1000, 1000))
        acl += "\nuser:100000:--x\nmask::--x"
        self.assertTrue(main.acl_allows_traversal(acl, 100000, 100000, 1000, 1000))
        self.assertFalse(main.acl_allows_traversal(acl.replace("mask::--x", "mask::rw-"), 100000, 100000, 1000, 1000))
        acl = "user::rwx\ngroup::--x\nother::---\nmask::--x"
        self.assertTrue(main.acl_allows_traversal(acl, 100000, 100000, 1000, 100000))
        self.assertFalse(main.acl_allows_traversal(acl, 100000, 100000, 1000, 1000))
        # A matching named user overrides other::x, even when the entry denies x.
        acl = "user::rwx\nuser:100000:---\ngroup::---\nmask::rwx\nother::r-x"
        self.assertFalse(main.acl_allows_traversal(acl, 100000, 100000, 1000, 1000))

    async def test_doctor_traversal_is_read_only_and_suggests_specific_acl(self):
        async def get_acl(cmd, timeout):
            other = "---" if cmd[-1] == "/home/example" else "r-x"
            return 0, f"user::rwx\ngroup::---\nother::{other}", ""
        with patch.object(main.Path, "stat", return_value=SimpleNamespace(st_uid=1000, st_gid=1000)), patch.object(main, "run_cmd", AsyncMock(side_effect=get_acl)) as command:
            checks = await main.check_rootfs_traversal(Path("/home/example/rootfs"), 100000, 100000)
        failures = [c for c in checks if c["status"] == "error"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["remediation"], "setfacl -m u:100000:--x -- /home/example")
        self.assertTrue(all(c.args[0][0] == "getfacl" for c in command.await_args_list))

    async def test_doctor_host_mapping_and_namespace_warning(self):
        restricted = False
        def read_text(path):
            if str(path) == "/proc/self/uid_map":
                return "1000 1000 1" if restricted else "0 0 4294967295"
            if str(path) == "/proc/self/status":
                return "NoNewPrivs:\t0\n"
            return "lxc.idmap = u 0 100000 65536\nlxc.idmap = g 0 200000 65536"
        with patch.object(main.Path, "read_text", read_text), patch.object(main, "run_lxc_cmd", AsyncMock(return_value=(0, "", ""))), patch.object(main, "check_rootfs_traversal", AsyncMock(return_value=[{"id": "directory_traversal", "status": "error", "message": "Denied", "remediation": "setfacl example"}])) as traversal:
            response = await self.client.get("/doctor")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["read_only"])
            self.assertEqual(response.json()["status"], "error")
            self.assertEqual(traversal.await_args.args[1:], (100000, 200000))
            restricted = True
            traversal.reset_mock()
            response = await self.client.get("/doctor")
            self.assertEqual(response.json()["status"], "warning")
            traversal.assert_not_awaited()

    async def test_doctor_rejects_unsafe_name_and_missing_config(self):
        self.assertEqual((await self.client.get("/doctor?name=../bad")).status_code, 422)
        with patch.object(main, "run_lxc_cmd", AsyncMock(return_value=(0, "", ""))), patch.object(main.Path, "is_file", return_value=False):
            self.assertEqual((await self.client.get("/doctor?name=missing")).status_code, 404)

    async def test_releases_filter_arch_variant_and_deduplicate(self):
        index = "\n".join([
            "alpine;3.23;amd64;default;date;/image/",
            "alpine;3.23;amd64;default;other-date;/image/",
            "alpine;3.24;amd64;default;date;/image/",
            "alpine;3.22;arm64;default;date;/image/",
            "alpine;edge;amd64;cloud;date;/image/",
            "ubuntu;noble;amd64;default;date;/image/",
            "invalid-row",
        ])
        with patch.object(main, "download_image_index", return_value=index):
            response = await self.client.get("/images/alpine/releases")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["releases"], ["3.23", "3.24"])
            response = await self.client.get("/images/alpine/releases?arch=arm64")
            self.assertEqual(response.json()["releases"], ["3.22"])

    async def test_releases_empty_and_upstream_failure(self):
        with patch.object(main, "download_image_index", return_value=""):
            self.assertEqual((await self.client.get("/images/alpine/releases")).json()["releases"], [])
        with patch.object(main, "download_image_index", side_effect=URLError("offline")):
            response = await self.client.get("/images/alpine/releases")
            self.assertEqual(response.status_code, 502)
            self.assertIn("retry", response.json()["detail"])

    async def test_release_query_validation(self):
        with patch.object(main, "download_image_index") as download:
            self.assertEqual((await self.client.get("/images/unknown/releases")).status_code, 400)
            self.assertEqual((await self.client.get("/images/alpine/releases?arch=invalid")).status_code, 422)
            download.assert_not_called()


class ShellBridgeTests(unittest.TestCase):
    """End-to-end shell session through the WebSocket pipe bridge.

    Uses a local lxc-attach shim that execs the requested command, so the
    real container is never touched. Regression for the silent-shell bug:
    output must flow back through the bridge for commands the shell runs.
    """

    def test_shell_bridge_returns_command_output(self):
        import tempfile
        import time
        from pathlib import Path
        from starlette.testclient import TestClient

        with tempfile.TemporaryDirectory() as bindir:
            shim = Path(bindir) / "lxc-attach"
            shim.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "while True:\n"
                "    line = sys.stdin.buffer.readline()\n"
                "    if not line:\n"
                "        break\n"
                "    sys.stdout.buffer.write(b'GOT:' + line)\n"
                "    sys.stdout.buffer.flush()\n"
            )
            shim.chmod(0o755)
            with patch.object(main, "LXC_PATH", bindir), \
                    patch.object(main, "require_websocket_access", lambda ws: True), \
                    patch.object(main, "_websocket_principal", lambda ws: main.Principal("raymond", os.getuid(), os.getgid(), "/home/raymond", "/bin/sh")), \
                    patch.object(main, "get_container_info", AsyncMock(return_value=main.ContainerResponse(name="demo", status="RUNNING"))):
                with TestClient(main.app) as client:
                    with client.websocket_connect("/containers/demo/shell?cols=100&rows=30") as ws:
                        deadline = time.monotonic() + 8
                        received = b""
                        ws.send_json({"type": "input", "data": "echo BRIDGE-$((6*7))\r"})
                        while b"GOT:" not in received and time.monotonic() < deadline:
                            received += ws.receive_bytes()
                        self.assertIn(b"GOT:echo BRIDGE-$((6*7))", received)
                        ws.send_json({"type": "close"})

    def test_shell_bridge_rejects_stopped_container(self):
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        with patch.object(main, "require_websocket_access", lambda ws: True), \
                patch.object(main, "_websocket_principal", lambda ws: main.Principal("raymond", os.getuid(), os.getgid(), "/home/raymond", "/bin/sh")), \
                patch.object(main, "get_container_info", AsyncMock(return_value=main.ContainerResponse(name="demo", status="STOPPED"))):
            with TestClient(main.app) as client:
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("/containers/demo/shell"):
                        pass


if __name__ == "__main__":
    unittest.main()
