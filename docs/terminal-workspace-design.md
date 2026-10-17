# Persistent Docked Terminal Workspace

## Overview

Replace the single modal shell with a persistent bottom-docked terminal workspace. Multiple sessions may be open for the same container, and each session has its own terminal buffer, WebSocket, shell process, shell selection, and reconnect lifecycle.

## Container actions

Each container row exposes a dedicated shell action:

- **Open shell** creates a new terminal session every time it is clicked.

Example:

```text
agent-hermes
[Open shell]
```

Opening another shell for a container never replaces an existing session.
All existing sessions remain visible as tabs in the dock.

## Dock layout

The bottom dock contains active sessions across all containers:

```text
┌─────────────────────────────────────────────────────────────┐
│ agent-hermes#1 × │ agent-hermes#2 ● │ rocky#1 ●       ⌃  × │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│ agent-hermes#2#                                             │
└─────────────────────────────────────────────────────────────┘
```

Each tab shows:

- Container name and session label.
- Connection state.
- Unread-output indicator.
- A close control for that session.

Dock controls:

- Resize handle on the top edge.
- Minimize collapses the dock without closing sessions.
- Close terminates only the active session.
- Close-all requires confirmation.

The default dock position is bottom, with its height persisted between visits.

## Session lifecycle

Session states are:

- `connecting`
- `connected`
- `reconnecting`
- `disconnected`
- `closed`

Browser refresh restores session metadata and reconnects active tabs. The backend keeps a disconnected shell alive for a short grace period, initially 60 seconds.

A session closes when the user closes its tab, confirms close-all, the shell exits, or the reconnect grace period expires.

## Shell selection and override

The new-session flow includes a shell selector:

```text
Shell
[ /bin/sh                       ▼ ]
```

The UI should detect available shells inside the target container. Typical choices include `/bin/sh`, `/bin/bash`, `/bin/fish`, `/bin/zsh`, `/usr/bin/bash`, and `/usr/bin/fish`. A custom absolute path may also be entered.

The session request carries the selected executable:

```json
{
  "container": "agent-hermes",
  "shell": "/bin/bash"
}
```

The backend must:

- Require an absolute executable path.
- Reject control characters and malformed paths.
- Verify that the executable exists and is executable inside the container.
- Set `SHELL` to the selected path.
- Launch the selected shell through the PTY relay.
- Avoid silently changing the requested shell; fallback to `/bin/sh` is only used when explicitly selected.

Arbitrary shell arguments are out of scope. The override selects an executable, not an arbitrary command line.

## Multiple sessions per container

Each session owns:

- A unique session ID, for example `agent-hermes:session-7f2c`.
- A WebSocket connection.
- A backend shell process.
- A terminal buffer and scrollback.
- A selected shell executable.
- A tab and reconnect lifecycle.

## Browser persistence

Persist session metadata, not terminal scrollback, in `localStorage`:

```json
{
  "sessions": [
    {
      "id": "agent-hermes:session-7f2c",
      "container": "agent-hermes",
      "shell": "/bin/bash",
      "label": "bash",
      "dock": "open"
    }
  ],
  "dockHeight": 420,
  "activeSession": "agent-hermes:session-7f2c"
}
```

Terminal scrollback remains in memory. Full browser-restart scrollback restoration is intentionally deferred.

## Recommended implementation order

1. Replace the single shell state with a session map.
2. Add a dedicated **Open shell** container action.
3. Build the dock and tab switching.
4. Add shell discovery and override validation.
5. Add backend reconnect grace periods.
6. Persist and restore session metadata.

## Acceptance criteria

- Multiple sessions can be opened for one container without replacing one another.
- Sessions for different containers share one dock and switch instantly.
- Minimizing the dock leaves shell processes alive.
- Closing a tab terminates only its shell.
- Refreshing the page restores tabs and reconnects within the grace period.
- Unavailable shell paths are rejected before launch with a useful error.
- A selected shell is actually used inside the container.
- Invalid paths and arbitrary shell arguments are rejected.
