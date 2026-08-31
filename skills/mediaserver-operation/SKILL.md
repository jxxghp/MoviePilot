---
name: mediaserver-operation
version: 1
description: >-
  Use this skill when the user asks to inspect, diagnose, or directly operate a
  configured Emby, Jellyfin, Plex, ZSpace, UGREEN, TrimeMedia, or Navidrome
  server. It discovers provider capabilities on demand and supports libraries,
  native movie/music search, episode coverage, recent media, resume state,
  playback sessions, statistics, scans, metadata refreshes, and provider play
  URLs without adding permanent tools.
allowed-tools: execute_command
---

# Media Server Operation

Use `scripts/mp-mediaserver.py`. The helper reads MoviePilot's local server
configuration and credentials itself. Never request, print, or pass a host,
username, password, API key, token, Cookie, or arbitrary URL.

## Boundary

- Keep `library.exists` in `moviepilot-api` for canonical duplicate checks. It
  aggregates configured servers and applies MoviePilot media identity and music
  matching rules.
- Use this Skill for server-native exploration, diagnostics, playback state,
  library browsing, scans, and metadata refreshes.
- A provider result is not automatically a MoviePilot transfer, subscription,
  or history fact. Use the appropriate MoviePilot API for those workflows.

## Discover First

```bash
python skills/mediaserver-operation/scripts/mp-mediaserver.py instances
python skills/mediaserver-operation/scripts/mp-mediaserver.py capabilities
python skills/mediaserver-operation/scripts/mp-mediaserver.py capabilities --server "living-room"
```

`capabilities` returns namespaced actions, argument requirements, providers, and
side-effect levels. Call it before using an unfamiliar server or advanced action.

## Call Shape

```bash
python skills/mediaserver-operation/scripts/mp-mediaserver.py call \
  --server "living-room" \
  --action activity.latest \
  --arguments '{"limit":20}'
```

The `--arguments` value must be one JSON object. List reads default to 50 items
and cap at 200.

## Actions

- Read: `server.statistics`, `server.users.count`,
  `server.user.library_folders`, `libraries.list`, `items.list`, `items.count`,
  `items.detail`, `items.movies.search`, `items.music.search`,
  `items.season_episodes`, `activity.latest`, `activity.resume`,
  `activity.backdrops`, `playback.sessions`, and `playback.url`.
- External side effects: `library.scan` and `metadata.refresh`.

Use the exact `server` and item/library IDs returned by earlier calls. Do not
invent IDs or reuse IDs across different server instances. `items.list` expects
`parent` for all video providers; Navidrome uses its single music library and
does not require a parent. `metadata.refresh` accepts an `items` array matching
MoviePilot's refresh item contract (`title`, `year`, `type`, `category`,
`target_path`).

Use `items.movies.search` for provider-native movie lookup,
`items.music.search` for a title/artist/album lookup, and
`items.season_episodes` when a direct server series ID or exact title is known.
These results describe one server only; use `library.exists` when the task needs
MoviePilot's canonical cross-server duplicate decision.

## Safety And Verification

- Before `library.scan`, confirm the exact server and scan scope/mode.
- Before `metadata.refresh`, confirm the exact item list because providers may
  perform a broad library refresh when a precise item cannot be mapped.
- After a scan or refresh, verify with `activity.latest`, `items.detail`, or the
  smallest relevant library query.
- If a provider does not advertise an action, report it as unsupported. Never
  fall back to raw HTTP, arbitrary SDK methods, or credentials copied from
  MoviePilot settings.
