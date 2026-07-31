---
name: moviepilot-cli
version: 6
description: >-
  Use this skill when the user asks to operate MoviePilot through the local
  `moviepilot tool` MCP CLI for normal product workflows: media search, torrent
  search, downloads, subscriptions, downloader tasks, library checks, sites,
  schedulers, workflows, and messages. Prefer dedicated skills for slash command
  dispatch, manual file organization or failed transfer retry, direct REST API
  calls, direct database SQL, browser operations, and restart/upgrade.
---

# MoviePilot CLI

> All script paths are relative to this skill file.

Use local `moviepilot tool ...` commands to interact with MoviePilot MCP tools.
The command reads the local MoviePilot configuration; do not ask the user for
`API_TOKEN`, database passwords, or a backend DSN during normal local use.

## Scope And Boundaries

This skill is for normal MoviePilot product operations exposed as MCP tools.
Choose other skills first when they match more precisely:

| Request | Preferred skill |
|---|---|
| Slash commands or plugin/system command dispatch | `command-dispatch` |
| Manual file organization | `organize-files` |
| Retry failed transfer history records | `transfer-failed-retry` |
| Direct REST endpoint not exposed by MCP tools | `moviepilot-api` |
| Direct SQL query or database update | `database-operation` |
| Restart, version check, or upgrade | `moviepilot-update` |
| Browser-only state, site login pages, screenshots, cookies | `browser-use` |

Use `moviepilot-api` only after `moviepilot tool list` and
`moviepilot tool show <command>` confirm that no MCP tool covers the required
operation. Use `database-operation` only when the task explicitly requires SQL
inspection or mutation, or when product tools/API cannot answer the data
question.

## Discover Commands

List all available commands: `moviepilot tool list`

Show parameters and usage for a specific command: `moviepilot tool show <command>`

The tool list includes tools declared by enabled plugins. Re-run `tool list` and
`tool show` after a plugin is enabled, disabled, reloaded, or reconfigured so the
command selection uses the refreshed runtime registry.

Always run `show <command>` before calling a command — parameter names are not inferable, do not guess.

## Command Groups

| Category | Commands |
|---|---|
| Media Search | search_media, recognize_media, query_media_detail, get_recommendations, search_person, search_person_credits |
| Torrent | search_torrents, get_search_results |
| Download | add_download_tasks, query_download_tasks, update_download_tasks, delete_download_tasks, query_downloaders |
| Subscription | add_subscribe, query_subscribes, update_subscribe, delete_subscribe, search_subscribe, query_subscribe_history, query_popular_subscribes, query_subscribe_shares |
| Library | query_library_exists, query_library_latest, transfer_file, scrape_metadata, query_transfer_history |
| Files | list_directory, query_directory_settings |
| Sites | query_sites, query_site_userdata, test_site, update_site, update_site_cookie |
| System | query_schedulers, run_scheduler, create_agent_task, query_agent_tasks, update_agent_task, run_agent_task, delete_agent_task, query_workflows, run_workflow, query_rule_groups, query_episode_schedule, send_message |

## Workflows

### Search and Download

#### 1. Search TMDB

Search for a movie or TV show by title: 
`moviepilot tool run search_media title="..." media_type="movie"`

If the user specifies a TV season, run Season Validation step first — the season number provided by the user may not match TMDB.

#### 2. Search torrents

Prefer `tmdb_id`; use `douban_id` only when `tmdb_id` is unavailable.

Omitting `sites=` uses the user's default sites. If the user specifies sites, first retrieve site IDs:
`moviepilot tool run query_sites`

Search torrents using default sites:
`moviepilot tool run search_torrents tmdb_id=791373 media_type="movie"`

Search torrents using user-specified sites (pass site IDs from `query_sites`):
`moviepilot tool run search_torrents tmdb_id=791373 media_type="movie" sites='1,3'`

When `search_torrents` returns:
1. **Stop** — do not call `get_search_results` yet.
2. Present all `filter_options` fields and every value within each field to the user verbatim.
3. Do not pre-select, summarize, or omit any field or value.
4. Wait for the user to select filters or confirm no filters are needed before moving to the next step.

#### 3. Get filtered results (only after user has responded to filter_options)

Run `moviepilot tool show get_search_results` to check available parameters. Filter logic: OR within a field, AND across fields.

Filter values must come from the `filter_options` returned by `search_torrents` — do not invent, translate, normalize, or use values from any other source. Note: `filter_options` keys are camelCase (e.g., `freeState`), but `get_search_results` params are snake_case (e.g., `free_state`).

Fetch results with selected filters:
`moviepilot tool run get_search_results resolution='1080p,2160p' free_state='免费,50%'`

To filter subtitle, audio, DIY, translation, or release notes that may appear outside the title, use `content_pattern`. It matches the torrent title, description, and labels while `title_pattern` continues to match the title only. Set `include_description=true` when the description is needed to explain why a result matched:
`moviepilot tool run get_search_results content_pattern='特效字幕|国语|DIY' include_description=true`

If empty, tell the user which filter to relax and ask before retrying.

#### 4. Present results as a numbered list

Show all results without pre-selection. Each row: index, title, size, seeders, resolution, release group, `volume_factor`, `freedate_diff`.

| `volume_factor` | Meaning |
|---|---|
| `免费` | Free download |
| `50%` | 50% download size |
| `2X` | Double upload |
| `2X免费` | Double upload + free |
| `普通` | No discount |

`freedate_diff`: remaining free window (e.g., `2天3小时`).

#### 5. Check before downloading

After the user picks torrents: Run **Check Library and Subscriptions** step.

If the media already exists in the library or is already subscribed, **stop** and report the finding to the user.

#### 6. Add download

Download one or more torrents (`torrent_url` comes from `get_search_results` output):
`moviepilot tool run add_download_tasks torrent_url="abc1234:1,def5678:2"`

#### Error handling

| Step | Action |
|---|---|
| `search_media` empty | Retry with alternative title (English/original), inform user. Still empty → ask for title or TMDB ID. |
| `search_torrents` empty | Inform user, ask whether to retry with different sites. |
| `get_search_results` empty | Do not silently broaden filters. Suggest which filter to relax, ask before retrying. |
| `add_download_tasks` fails | Run `query_downloaders` + `query_download_tasks` to diagnose, then report to user. |

### Add Subscription

1. Search for the media to get `tmdb_id`: Run `search_media`.
2. Run **Check Library and Subscriptions** step, if media already exists or is subscribed, **stop** and report to user.
3. If the user specifies a TV season, run Season Validation step first.

Subscribe to a movie or TV show:
`moviepilot tool run add_subscribe title="..." year="2011" media_type="tv" tmdb_id=42009`

Subscribe to a specific season:
`moviepilot tool run add_subscribe title="..." year="2011" media_type="tv" tmdb_id=42009 season=4`

Subscribe starting from a specific episode:
`moviepilot tool run add_subscribe title="..." year="2024" media_type="tv" tmdb_id=12345 season=1 start_episode=13`

### Manage Downloads

List download tasks and get hash for further operations:
`moviepilot tool run query_download_tasks status=downloading`

Use `status=completed` for tasks that are neither downloading nor paused in the downloader; use `status=all` to include every MoviePilot-tagged downloader task. Add `include_all_tags=true` when diagnosing tasks that do not have the MoviePilot built-in tag. Add `include_trackers=true` or query by `hash` when tracker URLs are needed.

Update a download task (supports start/stop, tags, speed limits, trackers, save path, category, ratio, and seeding time where the downloader supports them):
`moviepilot tool run update_download_tasks hash=<hash> action=stop upload_limit=512 download_limit=2048`

Add trackers to a download task:
`moviepilot tool run update_download_tasks hash=<hash> trackers='https://tracker.example/announce,udp://tracker.example:80/announce'`

Delete a download task (confirm with user first — irreversible):
`moviepilot tool run delete_download_tasks hash=<hash>`

Delete a download task and also remove its files (confirm with user first — irreversible):
`moviepilot tool run delete_download_tasks hash=<hash> delete_files=true`

### Manage Subscriptions

List active subscriptions:
`moviepilot tool run query_subscribes status=R`

Update subscription filters:
`moviepilot tool run update_subscribe subscribe_id=123 resolution="1080p"`

Only download full-season packs for a TV best-version subscription:
`moviepilot tool run update_subscribe subscribe_id=123 best_version=1 best_version_full=1`

Trigger a search for missing episodes (confirm with user first):
`moviepilot tool run search_subscribe subscribe_id=123`

Remove a subscription (confirm with user first):
`moviepilot tool run delete_subscribe subscribe_id=123`

### Manage Autonomous Agent Tasks

Use autonomous tasks only when the user explicitly requests delayed, recurring,
reminder, or monitoring behavior. Immediate work should run directly. Use the
MoviePilot `TZ` setting for local times.

Scheduled runs reuse the original Agent session context, but user-facing
messages are broadcast through MoviePilot's configured notification channels
instead of being tied to the channel that created the task. If the Agent sends
the complete result with a message tool during execution, it does not send the
same final reply again when the run finishes.

Autonomous task tools use the integer `task_id` returned by
`query_agent_tasks`. `query_schedulers` and `run_scheduler` are only for
MoviePilot system, plugin, and workflow runtime services and use string
`job_id` values; never mix these IDs or use those tools for autonomous tasks.

For a relative one-time request, use `date` with `delay_minutes`; MoviePilot
calculates and persists the exact run time:
`moviepilot tool run create_agent_task name="检查电影资源" content="搜索电影《示例电影》是否有资源并报告，不要自动下载。" trigger_type=date delay_minutes=30`

For a one-time task at a fixed time, use `date` with an ISO 8601 `trigger`:
`moviepilot tool run create_agent_task name="今晚检查资源" content="检查目标电影是否有资源并报告。" trigger_type=date trigger="2026-07-19 20:30:00"`

For recurring work, use a standard five-field cron expression. This example
runs every day at 20:30:
`moviepilot tool run create_agent_task name="每日资源检查" content="检查目标电影是否有资源并报告。" trigger_type=cron trigger="30 20 * * *"`

List tasks and inspect `next_run_at` and the latest result:
`moviepilot tool run query_agent_tasks`

Pause or resume a task:
`moviepilot tool run update_agent_task task_id=1 enabled=false`

Queue an enabled task for immediate execution without waiting in the current
Agent turn:
`moviepilot tool run run_agent_task task_id=1`

Delete a task only after confirming permanent removal with the user:
`moviepilot tool run delete_agent_task task_id=1`

### Check Library and Subscriptions

Run before any download or subscription to avoid duplicates.

Check if the media already exists in the library:
`moviepilot tool run query_library_exists tmdb_id=123456 media_type="movie"`

Check if the media is already subscribed:
`moviepilot tool run query_subscribes tmdb_id=123456`

### Season Validation

Mandatory when user specifies a season. Productions sometimes release a show in multiple parts under one TMDB season; online communities and torrent sites may label each part as a separate "season".

#### 1. Verify season exists

Fetch media detail to check available seasons:
`moviepilot tool run query_media_detail tmdb_id=<id> media_type="tv"`

Compare `season_info` with the user's requested season:
1. If the season exists in `season_info` → use that season number directly and return to the calling workflow.
2. If the season does not exist → the user's "season" likely maps to a later episode range within an existing TMDB season. Note the latest (highest-numbered) season from `season_info`, then continue to next step.

#### 2. Identify the correct episode range

Fetch episode schedule for the latest season from `season_info`:
`moviepilot tool run query_episode_schedule tmdb_id=<id> season=<latest_season_number>`

Use `air_date` to find a block of recently-aired episodes that likely corresponds to what the user calls the missing season. Look for a gap in `air_date` between episodes — the gap indicates a part break, and the episodes after the gap are what the user likely refers to as the next "season". For example, if TMDB Season 1 has episodes 1–24 and there is a multi-month gap between episode 12 and 13, then episodes 13–24 correspond to the user's "Season 2". If no such gap exists, tell user content is unavailable. Otherwise confirm the episode range with user.

## Error handling

Missing configuration or authentication failure: run `moviepilot doctor` to
verify the local MoviePilot installation and settings. Plugin-only log findings
remain visible but do not by themselves downgrade the overall Doctor status.
Do not ask the user to paste the API key into the prompt for local CLI usage.
