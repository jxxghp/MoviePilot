---
name: moviepilot-update
version: 4
description: Use this skill to check MoviePilot versions, inspect Release update state, download a Release update in the background, confirm installation, restart MoviePilot, or retain the existing Dev branch update flow. Prefer the built-in system APIs instead of container commands or manual file replacement.
---

# MoviePilot Update

> All script paths are relative to this skill file.

Use this skill for MoviePilot restart and upgrade operations.

## Setup

This skill reuses the `moviepilot-api` client. When running inside the MoviePilot project, the API client imports `app.runtime.config.settings` and reads the local host, port, and API token directly. Do not ask the user for `API_TOKEN`.

## Preferred Commands

### Check versions

```bash
python scripts/mp-update.py versions
```

This calls `GET /api/v1/system/versions`.

### Restart MoviePilot

```bash
python scripts/mp-update.py restart
```

This calls `GET /api/v1/system/restart`.

### Release update

Check for a stable Release and inspect current progress:

```bash
python scripts/mp-update.py check
python scripts/mp-update.py status
```

Start the background download. This does not restart MoviePilot:

```bash
python scripts/mp-update.py download
```

After `status` reports `state=ready`, installation requires a separate explicit confirmation:

```bash
python scripts/mp-update.py install
```

`install` writes the verified install intent and restarts MoviePilot. Do not call it until the user explicitly confirms the restart.

### Dev update and restart

```bash
python scripts/mp-update.py upgrade dev
```

Dev mode retains the existing `POST /api/v1/system/upgrade` path with body `"dev"`. It tracks the current v3 development branch during restart. Release mode is no longer accepted by that endpoint.

## Direct API Examples

```bash
python ../moviepilot-api/scripts/mp-api.py GET /api/v1/system/restart
python ../moviepilot-api/scripts/mp-api.py POST /api/v1/system/update/check
python ../moviepilot-api/scripts/mp-api.py GET /api/v1/system/update/status
python ../moviepilot-api/scripts/mp-api.py POST /api/v1/system/update/download
python ../moviepilot-api/scripts/mp-api.py POST /api/v1/system/update/install
python ../moviepilot-api/scripts/mp-api.py POST /api/v1/system/upgrade --json '"dev"'
```

## Notes

- These operations require administrator authentication.
- Only restart, Release installation, and Dev upgrade interrupt the current agent session. Checking and downloading remain online.
- Prefer the API flow above. Only fall back to manual container commands when the API is unavailable.
