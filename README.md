# LXC Manager

FastAPI application with a browser dashboard and REST API for rootless LXC container management.

## Features

- **List containers** with status, PID, IP addresses
- **Create containers** from 20+ base image distributions (Ubuntu, Alpine, Debian, Fedora, etc.)
- **Start/stop/restart** containers
- **Delete containers** (with force option)
- **Resource metrics** for running containers:
  - CPU usage (%)
  - Memory usage/limit (bytes)
  - Disk I/O (read/write bytes)
  - Network I/O (RX/TX bytes)
  - Snapshots list
- **Snapshot management** (create/delete)
- **Console output** (last 100 lines)
- **Async operations** throughout
- **No persistence layer** - queries LXC directly
- **Linux-account authentication** - login delegates username/password verification to PAM
- **OpenAPI/Swagger** docs at `/docs`

## Requirements

- LXC 7.0+ CLI tools (`lxc-ls`, `lxc-create`, `lxc-start`, `lxc-stop`, `lxc-destroy`, `lxc-attach`, `lxc-info`, `lxc-snapshot`)
- Rootless LXC configured:
  - `/etc/subuid` and `/etc/subgid` with user mappings
  - `/etc/lxc/lxc-usernet` with `@lxc veth lxcbr0 10`
  - User in `lxc` group
  - `~/.config/lxc/default.conf` with idmap entries
  - `lxc-net` service running (bridge `lxcbr0`)

## Quick Start

```bash
cd /home/raymondcoetzee/.kiro/crew/workspace/lxc-manager

make install  # Create .venv and install runtime dependencies
make test     # Install test dependencies and run isolated tests (no real containers)
make start    # Serve dashboard and API at http://127.0.0.1:8080

# Optional: automatic reload or a different port
make dev
make start PORT=8081
```

Run `make install`; it installs the runtime dependencies and selects the
reviewed PAM service template for Debian-family, Red Hat-family, or Arch-family
systems. Installation of `/etc/pam.d/lxc-manager` uses `sudo` when needed and
fails clearly on unsupported distributions. Then run `make start`. The app
serves on loopback by default, and all dashboard/API routes other than health
and the login endpoints require a valid Linux account. Interactive shell and
container web-app routes additionally enforce loopback requests. See
[`docs/auth-design.md`](docs/auth-design.md) for the session, CSRF,
authorization, and deployment details.

Open [the dashboard](http://127.0.0.1:8080/) to select an image, create a container,
start/stop/restart/delete containers, inspect logs, and create/delete snapshots.
Running containers show CPU, memory, and cumulative disk/network I/O, refreshed
every 10 seconds while the tab is visible. Missing metrics are shown as unavailable.
CPU is sampled over an interval; 100% represents one fully utilized core.
Snapshots can be listed for stopped containers too; creation support depends on
LXC's storage backend and container state.

The frontend uses HTML, CSS, vanilla JavaScript, and locally vendored xterm.js
served by FastAPI; no Node.js or frontend build is needed. API docs remain at
`/docs`. Run as the user with the
rootless LXC setup. The Makefile defaults to loopback because the current release
is intended for local-only access; authentication delegates to the host's Linux
accounts through PAM.
Distribution choices are configured. The release dropdown loads the live LXC
system image catalog when the distribution or architecture changes, filtering
for that distribution, architecture, and the default image variant. Catalog
errors offer a retry button; creation stays disabled until a release is selected.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Browser dashboard |
| GET | `/login` | Linux account login page |
| POST | `/auth/login` | Authenticate through the configured PAM service |
| GET | `/auth/status` | Current session status |
| POST | `/auth/logout` | Revoke the current session |
| GET | `/health` | Health check |
| GET | `/doctor` | Read-only rootless setup and traversal checks |
| GET | `/doctor?name=agent-hermes` | Check container mappings and rootfs parent permissions |
| GET | `/images` | List available base images |
| GET | `/images/{distro}/releases?arch=amd64` | Live releases available for a distribution and architecture |
| GET | `/containers` | List all containers |
| GET | `/containers/{name}` | Get container details |
| GET | `/containers/{name}/metrics` | Get resource metrics |
| GET | `/containers/{name}/console` | Get console output |
| GET | `/containers/{name}/shells` | Available executable shells in a running container |
| WS | `/containers/{name}/shell` | Authenticated reconnectable interactive shell session |
| ALL | `/containers/{name}/web/{port}/{path}` | Authenticated local reverse proxy to a container HTTP app |
| POST | `/containers` | Create container |
| POST | `/containers/{name}/start` | Start container |
| POST | `/containers/{name}/stop` | Stop container |
| POST | `/containers/{name}/restart` | Restart container |
| POST | `/containers/{name}/snapshot` | Create snapshot |
| GET | `/containers/{name}/snapshots` | List snapshots (including stopped containers) |
| DELETE | `/containers/{name}` | Delete container |
| DELETE | `/containers/{name}/snapshots/{snap}` | Delete snapshot |

## Example Usage

```bash
# List available images
curl http://localhost:8080/images

# Create an Alpine container
curl -X POST http://localhost:8080/containers \
  -H "Content-Type: application/json" \
  -d '{"name": "my-alpine", "distro": "alpine"}'

# Start it
curl -X POST http://localhost:8080/containers/my-alpine/start

# Get metrics
curl http://localhost:8080/containers/my-alpine/metrics

# Create snapshot
curl -X POST http://localhost:8080/containers/my-alpine/snapshot

# Stop and delete
curl -X POST http://localhost:8080/containers/my-alpine/stop
curl -X DELETE http://localhost:8080/containers/my-alpine
```

## Container Create Options

```json
{
  "name": "my-container",      // Required: alphanumeric, hyphen, underscore
  "distro": "ubuntu",          // Required: one of 20+ distros
  "release": "resolute",       // Optional: specific release (uses default)
  "arch": "amd64"              // Optional: amd64, arm64, i386
}
```

## Supported Distributions

AlmaLinux, Alpine, ALT, AmazonLinux, ArchLinux, BusyBox, CentOS, Debian, Devuan, Fedora, Kali, Linux Mint, NixOS, openEuler, openSUSE, OpenWrt, Oracle Linux, Plamo, RockyLinux, Slackware, Ubuntu, VoidLinux

## Systemd Service

See `lxc-manager.service` for production deployment.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│   Client    │────▶│  FastAPI     │────▶│  lxc-* CLI  │
│  (REST)     │     │  (async)     │     │  (subproc)  │
└─────────────┘     └──────────────┘     └─────────────┘
                           │
                           ▼
                    ┌──────────────┐
                    │  Rootless    │
                    │  LXC Containers │
                    │  (~/.local/  │
                    │   share/lxc) │
                    └──────────────┘
```

## Notes

### Dashboard interaction

The dashboard uses persistent container rows: polling updates metric text in place
and preserves keyboard focus, expanded details, and loaded logs. Previous readings
remain visible during refresh and are marked stale when metrics cannot be fetched.
Use **Live updates** to pause polling; hidden browser tabs do not poll.

**Create container** opens a modal side panel that preserves input when closed.
**Details** expands I/O, snapshots, and logs; snapshots and logs load on demand.
Secondary actions are in each container's overflow menu. Diagnostics open in a
separate side panel, and notifications overlay the page without moving the list.
On smaller screens, rows rearrange into cards. Reduced-motion preferences are respected.

Dashboard polling uses `/containers/{name}/metrics?include_snapshots=false` to
avoid expensive snapshot queries. The API default still includes snapshots for
existing clients. Run `make test-ui` (Node.js required only for these tests) for
the frontend state regression checks. A browser visual review is separate from
these DOM-adapter tests.

### Shells and container web apps

Sign in with a valid Linux account, then use **Open shell** from a running
container's row, choose an executable shell, and work in the persistent bottom-docked
xterm.js terminal workspace. Each click creates a separate session; dock tabs
switch between sessions without closing them. Keystrokes, ANSI colors, cursor addressing, terminal resizing,
Ctrl+C, and full-screen programs are sent through a WebSocket to a shell launched
inside its own PTY with `TERM=xterm-256color`. Ctrl+Shift+C/V copies and pastes.
Minimizing the dock leaves sessions alive, browser refresh reconnects persisted
tabs when possible, and disconnected shells remain available for 60 seconds.
Shell paths must be absolute executable paths inside the container; arbitrary
shell arguments are not accepted. Shell discovery prefers Bash, zsh, or fish
over `/bin/sh` because those interactive shells provide tab completion; `/bin/sh`
remains the fallback for minimal images.

Use **Open web app** and enter the port used by an HTTP service in the running
container. The manager discovers the container's private IPv4 address and
reverse-proxies the app through a local-only route. Relative paths,
HTTP redirects, and cookie paths are rewritten for this route. Apps that hardcode
an absolute hostname, use external WebSocket origins, or require HTTPS may need
their own reverse-proxy configuration. Only private container addresses are
accepted, and no proxy route is exposed on non-loopback clients.

Use **Run checks** in the Rootless LXC doctor panel or **Diagnose** on a container
to inspect setup. A blocked parent directory produces a suggested `setfacl`
command granting only traversal to that container's mapped root UID. Commands
are never applied automatically. Checks evaluate POSIX access ACLs including the
ACL mask; run the app on the host for accurate host UID permissions. The doctor
does not test startup or change containers, and does not resolve included idmaps
or non-directory storage backends. Its `ok` status only covers the listed checks.

- Metrics require container to be RUNNING
- Disk/network metrics read from cgroup v2 and /proc inside container
- Snapshots use LXC's native snapshot mechanism
- No database - all state queried live from LXC
# lxc-manager
