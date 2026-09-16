---
name: moviepilot-update
version: 5
description: Use this skill to check MoviePilot versions, inspect Release update state, download a Release update in the background, confirm installation, restart MoviePilot, or retain the existing Dev branch update flow. Prefer the built-in system APIs instead of container commands or manual file replacement.
allowed-tools: moviepilot_api
allowed-api-operations: >-
  system.versions system.update.status system.update.check system.update.download
  system.restart system.update.install system.upgrade.dev
---

# MoviePilot Update

Use this skill for MoviePilot restart and upgrade operations.

Use the built-in `moviepilot_api` tool only. The host selects fixed API routes, authenticates with the trusted Agent identity, and applies administrator and confirmation policy. Never request or pass an API token, URL, HTTP method, shell command, or legacy helper script.

## Operations

### Check versions

```json
{"operation_id":"system.versions","path_params":{},"query":{},"body":{}}
```

This read-only operation lists available MoviePilot releases.

### Restart MoviePilot

```json
{"operation_id":"system.restart","path_params":{},"query":{},"body":{}}
```

Restart requires explicit confirmation and interrupts the current Agent session.

### Release update

Check for a stable Release and inspect current progress:

```json
{"operation_id":"system.update.check","path_params":{},"query":{},"body":{}}
{"operation_id":"system.update.status","path_params":{},"query":{},"body":{}}
```

Start the background download. This does not restart MoviePilot:

```json
{"operation_id":"system.update.download","path_params":{},"query":{},"body":{}}
```

After `status` reports `state=ready`, installation requires a separate explicit confirmation:

```json
{"operation_id":"system.update.install","path_params":{},"query":{},"body":{}}
```

`install` writes the verified install intent and restarts MoviePilot. Do not call it until the user explicitly confirms the restart.

### Dev update and restart

```json
{"operation_id":"system.upgrade.dev","path_params":{},"query":{},"body":"dev"}
```

The body must be the exact JSON string `"dev"`. Stable Release updates must use check, download, status, and install instead.

## Notes

- All operations require a MoviePilot administrator or a verified notification-channel administrator. The host performs authorization; the model must never invent an administrator flag.
- Only restart, Release installation, and Dev upgrade interrupt the current agent session. Checking and downloading remain online.
- Prefer the API flow above. Only fall back to manual container commands when the API is unavailable.
