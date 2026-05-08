---
name: moviepilot-update
version: 2
description: Use this skill when you need to check MoviePilot versions, restart MoviePilot, or trigger a MoviePilot upgrade. Prefer the built-in system APIs instead of docker commands or manual file replacement. If auto-update on restart is already enabled, just restart. If it is disabled, call the upgrade API so MoviePilot performs a one-shot upgrade and restart.
---

# MoviePilot Update

> All script paths are relative to this skill file.

Use this skill for MoviePilot restart and upgrade operations.

## Setup

This skill reuses the `moviepilot-api` client configuration.

Configure host and API key once:

```bash
python ../moviepilot-api/scripts/mp-api.py configure --host http://localhost:3000 --apikey <API_TOKEN>
```

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

### Upgrade and restart MoviePilot

Release mode:

```bash
python scripts/mp-update.py upgrade
```

Dev mode:

```bash
python scripts/mp-update.py upgrade dev
```

This calls `POST /api/v1/system/upgrade`.

Behavior:

- If `MOVIEPILOT_AUTO_UPDATE` is already enabled (`release` or `dev`), MoviePilot only triggers a restart and lets the normal startup flow perform the upgrade.
- If `MOVIEPILOT_AUTO_UPDATE` is disabled, MoviePilot writes a one-shot upgrade flag, restarts itself, performs that single upgrade during startup, and then continues running without changing the persisted auto-update setting.

## Direct API Examples

```bash
python ../moviepilot-api/scripts/mp-api.py GET /api/v1/system/restart
python ../moviepilot-api/scripts/mp-api.py POST /api/v1/system/upgrade --json '"release"'
python ../moviepilot-api/scripts/mp-api.py POST /api/v1/system/upgrade --json '"dev"'
```

## Notes

- These operations require administrator authentication.
- Restart or upgrade will interrupt the current agent session. Do not rely on post-restart follow-up steps in the same run.
- Prefer the API flow above. Only fall back to manual container commands when the API is unavailable.
