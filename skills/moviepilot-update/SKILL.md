---
name: moviepilot-update
version: 7
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

Check for the highest stable V3 Release and inspect current progress. A manual check may replace an older prepared target with a newer version; failed checks preserve downloaded packages:

```json
{"operation_id":"system.update.check","path_params":{},"query":{},"body":{}}
{"operation_id":"system.update.status","path_params":{},"query":{},"body":{}}
```

When `supports_batch_update=true`, prepare the latest backend, matching frontend, and site resources together. This does not restart MoviePilot:

```json
{"operation_id":"system.update.download","path_params":{},"query":{},"body":{"target":"all"}}
```

After every non-idle item in `updates` reports `state=ready` and no item has a check error, installation requires a separate explicit confirmation:

```json
{"operation_id":"system.update.install","path_params":{},"query":{},"body":{"target":"all"}}
```

`install` writes one verified install intent and restarts MoviePilot once. Do not call it until the user explicitly confirms the restart. Use `target=application` or `target=resources` for a specifically requested single-target update. On older hosts without `supports_batch_update`, retain the single-target flow; an omitted target defaults to `application`.

### Dev update and restart

```json
{"operation_id":"system.upgrade.dev","path_params":{},"query":{},"body":"dev"}
```

The body must be the exact JSON string `"dev"`. Stable Release updates must use check, download, status, and install instead.

## Notes

- All operations require a MoviePilot administrator or a verified notification-channel administrator. The host performs authorization; the model must never invent an administrator flag.
- Only restart, Release installation, and Dev upgrade interrupt the current agent session. Checking and downloading remain online.
- Scheduled checks preserve downloaded packages; explicit checks refresh an older prepared target only when a higher version exists. Active downloads and installations remain protected. A previously overwritten resource state can recover to `ready` after validating the matching staged package; use `updates` to inspect each target and request installation confirmation instead of downloading it again.
- Prefer the API flow above. Only fall back to manual container commands when the API is unavailable.
