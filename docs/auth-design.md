# Whole-application authentication design

## Goal

Require a valid Linux account before any browser or API user can use LXC
Manager, while delegating password verification and account policy to the
host's existing Linux authentication stack. The application now implements
this boundary and remains intended for loopback deployment by default.

## Proposed boundary

This design is now implemented in `main.py` with `python-pam` as the PAM
binding. `make install` installs the runtime dependency and selects the PAM
service template for Debian-family, Red Hat-family, or Arch-family systems;
the application fails closed when the binding or service is unavailable.

The application should continue to run as an unprivileged service account (or
as the single Linux user who owns the rootless LXC containers). Authentication
proves who is using the web application; it does not magically change the
Unix UID under which LXC commands run. If multiple Linux users must manage
different rootless container sets, use separate service instances or a narrow,
carefully reviewed privileged broker rather than running the web process as
root.

All routes except an explicitly minimal health endpoint should pass through a
single authentication dependency/middleware. That includes REST mutations,
the dashboard, shell discovery, the shell WebSocket handshake, and the
container web-app proxy. The health endpoint should disclose only process
health and should not expose container names, usernames, or authentication
state.

## Login flow

1. `GET /login` serves a small login page containing username and password
   fields. The browser must submit over HTTPS when the app is ever exposed
   beyond loopback; loopback-only deployment remains the default.
2. `POST /auth/login` accepts a username and password as JSON.
   First resolve the username through NSS (`pwd.getpwnam`) and apply a local
   account policy: account exists, is not disabled by policy, and has an
   allowed login shell. Then authenticate it through a dedicated PAM service,
   for example `lxc-manager`, whose configuration includes the distribution's
   normal system authentication and account modules.
3. Run the blocking PAM call in a worker thread. Never read `/etc/shadow`,
   invoke `su`/`login`, compare password hashes in Python, or log credentials.
   The configured PAM service is the authority for password and account policy;
   the application also requires an existing NSS user with an executable login
   shell.
4. On success, issue a cryptographically random opaque session ID. Store only
   a hash of that ID server-side, associated with the Linux username, creation
   time, last activity, and an expiry time. Set it in an HTTP-only, `Secure`
   (when HTTPS), `SameSite=Strict` cookie with `Path=/`.
5. Return the same generic failure response for unknown users, bad passwords,
   locked accounts, and disallowed accounts. Add per-source and per-account
   rate limiting with exponential backoff; do not reveal whether a username
   exists.

`POST /auth/logout` revokes the current session. Password or PAM policy changes
take effect on the next login; sessions are deliberately short-lived. The
implementation uses a 30-minute idle timeout and an 8-hour absolute lifetime.

## Request and WebSocket enforcement

Use one dependency such as `require_authenticated_user` for HTTP routes. It
must load the session, reject expired/revoked sessions, refresh idle expiry,
and attach a small principal object to the request. The WebSocket endpoint
must validate the same cookie before `accept()`; a cookie check only in the
frontend is not an authorization boundary.

All state-changing HTTP requests need CSRF protection. Use a per-session
CSRF token in a non-HTTP-only cookie plus a matching custom request header, and
validate `Origin`/`Referer` for browser requests. `SameSite=Strict` is useful
defense in depth, not a replacement for CSRF validation. WebSocket upgrades
should validate the expected `Origin` and the authenticated session.

## Linux/PAM integration

The `install-pam` target reads `/etc/os-release`, selects one of the reviewed
templates under `docs/pam/`, and installs it as `/etc/pam.d/lxc-manager` using
root or `sudo`. It supports Debian-family, Red Hat-family, and Arch-family
distributions and fails closed for anything else. Review the selected template
and its distribution's PAM policy before production use. The Python integration
is isolated behind `LinuxAuthenticator` so tests can use a fake authenticator
and production uses the maintained `python-pam` binding.

The authenticator should return only a success/failure result and a normalized
username. It should not return password material. Resolve the final principal
with NSS after PAM success and record the numeric UID/GID for audit context.
If the product later needs per-user LXC isolation, that must be implemented as
a separate execution/authorization design; PAM authentication alone is not
permission delegation.

## Authorization and audit

Authentication answers “who is this?” Authorization should initially be
simple and explicit: a configured allowlist of Linux usernames or groups may
use LXC Manager. Group membership should come from NSS, not from a browser
claim. Every destructive action (delete, stop, snapshot deletion), shell
creation, proxy access, login failure, logout, and authorization failure
should emit a structured audit event containing timestamp, username/UID when
known, action, container, request ID, and outcome. Never include passwords,
session IDs, or shell contents in logs.

## Deployment and verification plan

- Keep `127.0.0.1` as the default bind address and reject non-loopback traffic
  unless an explicit deployment mode enables TLS and trusted proxy handling.
- The application includes the PAM dependency and an injectable authenticator
  seam; document the package names needed by each supported distribution.
- Add tests for successful PAM login, bad/unknown/locked accounts, session
  expiry and revocation, CSRF rejection, non-loopback rejection, REST route
  coverage, and WebSocket authentication before acceptance.
- Add an end-to-end host test using a dedicated throwaway Linux account. Do
  not use the developer's normal password in automated tests.
- During rollout, fail closed if PAM configuration is missing or malformed;
  never silently fall back to the old access code or to unauthenticated mode.
