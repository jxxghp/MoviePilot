---
name: downloader-operation
version: 1
description: >-
  Use this skill when the user asks to inspect, diagnose, or directly control a
  configured qBittorrent, Transmission, or rTorrent instance. It exposes
  provider capabilities on demand without adding permanent Agent tools, and is
  suitable for task files and selection, trackers, peers, queue order, limits,
  tags, locations, rechecks, direct provider submissions, or batch task control.
allowed-tools: execute_command
---

# Downloader Operation

Use `scripts/mp-downloader.py`. The helper reads MoviePilot's local downloader
configuration and credentials itself. Never request, print, or pass a host,
username, password, API key, Cookie, or arbitrary URL.

## Boundary

- Prefer `moviepilot-api` for ordinary MoviePilot acquisition workflows,
  especially site search, `download.add`, subscriptions, transfer, history, and
  canonical library checks.
- Use this Skill for downloader-native inspection, diagnosis, advanced task
  properties, and an explicit request to operate the provider directly.
- `tasks.add.direct` bypasses MoviePilot download history, site Cookie handling,
  path selection, duplicate checks, and transfer orchestration. Use it only when
  the user explicitly wants direct provider submission.
- Paths passed to `tasks.location.set` and `tasks.add.direct` are downloader-side
  paths, not MoviePilot storage paths.

## Discover First

List configured instances without secrets:

```bash
python skills/downloader-operation/scripts/mp-downloader.py instances
```

List the actions supported by all providers or one configured client:

```bash
python skills/downloader-operation/scripts/mp-downloader.py capabilities
python skills/downloader-operation/scripts/mp-downloader.py capabilities --client "main-qb"
```

Do not guess a provider-specific action. Call `capabilities` when the current
instance, provider, argument contract, or side-effect level is uncertain.

## Call Shape

```bash
python skills/downloader-operation/scripts/mp-downloader.py call \
  --client "main-qb" \
  --action tasks.list \
  --arguments '{"status":"downloading","limit":20}'
```

The `--arguments` value must be one JSON object. Large reads are paged with
`offset` and `limit`; the default limit is 50 and the maximum is 200.

## Core Actions

- Read: `tasks.list`, `tasks.files`, `tasks.trackers`, `tasks.tags.get`,
  `tasks.peers`, `session.stats`, `session.speed_limits.get`,
  `session.details`, `session.content_layout`.
- Reversible writes: `tasks.start`, `tasks.stop`, `tasks.recheck`,
  `tasks.reannounce`, `tasks.queue.move`, `tasks.properties.set`,
  `tasks.files.selection.set`, `tasks.force_start.set`, `tasks.location.set`,
  `tasks.category.set`, `tasks.tags.set`, `tasks.trackers.update`,
  `session.speed_limits.set`.
- External/destructive: `tasks.add.direct`, `tasks.delete`.

For task actions, use `task_id` for one hash/ID or `task_ids` for a batch. Before
deleting data, confirm the exact client, tasks, and `delete_files=true`. Before a
direct add, confirm the exact magnet/URL or local torrent file, client, paused
state, provider path, tags, and category.

For `tasks.files.selection.set`, pass provider file indexes from `tasks.files`
through `wanted_file_ids` and/or `unwanted_file_ids`; never infer indexes from
filenames alone. `session.details` is Transmission-only and
`session.content_layout` is qBittorrent-only.

## Verification

After a write, query the smallest relevant state: `tasks.list` for status and
properties, `tasks.files` for file priority, `tasks.trackers` for trackers, or
`session.speed_limits.get` for global limits. Report unsupported provider
capabilities explicitly instead of falling back to raw HTTP or arbitrary SDK
method calls.
