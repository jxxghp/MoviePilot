---
name: moviepilot-api
version: 2
description: >-
  Use this skill when you need to call MoviePilot REST API endpoints directly
  with the bundled Python client. Covers MoviePilot HTTP endpoints across media
  search, downloads, subscriptions, library management, site management, system
  administration, plugins, workflows, and more. Prefer `moviepilot-cli` for
  normal local MCP tool workflows; use this skill when the user explicitly asks
  for HTTP API access, when an endpoint is not exposed as an MCP tool, or when
  running in an environment where direct REST calls are the appropriate bridge.
---

# MoviePilot REST API

> All script paths are relative to this skill file.

Use `scripts/mp-api.py` to call any MoviePilot REST API endpoint directly.

## Scope And Boundaries

This skill is the REST API bridge. It is implemented as a Python script and is
useful when the agent needs endpoint-level coverage beyond the local
`moviepilot tool` MCP CLI.

Choose other skills first when they match more precisely:

| Request | Preferred skill |
|---|---|
| Normal local MoviePilot product operation exposed as an MCP tool | `moviepilot-cli` |
| Direct SQL query or database update | `database-operation` |
| Restart, version check, or upgrade | `moviepilot-update` |
| Slash commands or plugin/system command dispatch | `command-dispatch` |
| Browser-only state, site login pages, screenshots, cookies | `browser-use` |

Do not use this skill just because MoviePilot is mentioned. Use it when the
task specifically needs a REST endpoint, token-query endpoint, or API behavior
that the CLI/MCP tools do not expose.

## Setup

When the script runs inside the MoviePilot project, it imports `app.core.config.settings` and reads `settings.HOST`, `settings.PORT`, and `settings.API_TOKEN` directly. Do not ask the user for `API_TOKEN`, and do not copy API keys into the prompt.

Configuration priority:

1. CLI flags: `--host`, `--apikey`
2. Environment variables: `MP_HOST`, `MP_API_KEY`
3. Local MoviePilot settings
4. Legacy config file: `~/.config/moviepilot_api/config`

Use `configure` only as a legacy fallback outside the MoviePilot project, and avoid it in normal agent workflows because it persists a long-lived API key to disk.

## How to Call APIs

### General syntax

```
python scripts/mp-api.py <METHOD> <PATH> [key=value ...] [--json '<body>']
```

### Authentication

- By default, the script auto-loads the local key and sends it via the `X-API-KEY` header.
- For endpoints suffixed with `2` (e.g. `/api/v1/dashboard/statistic2`), use `--token-param` to send the key as `?token=`.
- Both methods validate against the same `API_TOKEN` value.
- Never print, summarize, or ask the user to paste the API key unless the script is being used outside the local project and no safer configuration source is available.

### Examples

```bash
# GET with query params
python scripts/mp-api.py GET /api/v1/media/search title="Avatar" type="movie"

# POST with JSON body
python scripts/mp-api.py POST /api/v1/download/add --json '{"torrent_url":"abc1234:1"}'

# DELETE
python scripts/mp-api.py DELETE /api/v1/subscribe/123

# Endpoints that require ?token= auth
python scripts/mp-api.py GET /api/v1/dashboard/statistic2 --token-param
```

## Complete API Reference

All endpoints are under the base URL `{MP_HOST}`. Path parameters are shown as `{param}`.

---

### Media Search (13 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/media/search` | Search media/person by title. Params: `title` (required), `type`, `page`, `count` |
| GET | `/api/v1/media/recognize` | Recognize media from torrent title. Params: `title` (required), `subtitle` |
| GET | `/api/v1/media/recognize2` | Recognize media (API_TOKEN auth, use `--token-param`). Params: `title`, `subtitle` |
| GET | `/api/v1/media/recognize_file` | Recognize media from file path. Params: `path` (required) |
| GET | `/api/v1/media/recognize_file2` | Recognize file (API_TOKEN auth). Params: `path` |
| POST | `/api/v1/media/scrape/{storage}` | Scrape media metadata. Body: FileItem JSON |
| GET | `/api/v1/media/category/config` | Get category strategy config |
| POST | `/api/v1/media/category/config` | Save category strategy config. Body: CategoryConfig |
| GET | `/api/v1/media/category` | Get auto-categorization config |
| GET | `/api/v1/media/group/seasons/{episode_group}` | Get episode group seasons |
| GET | `/api/v1/media/groups/{tmdbid}` | Get media episode groups |
| GET | `/api/v1/media/seasons` | Get media season info. Params: `mediaid`, `title`, `year`, `season` |
| GET | `/api/v1/media/{mediaid}` | Get media detail. Params: `type_name` (required: movie/tv), `title`, `year` |

### TMDB (8 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/tmdb/seasons/{tmdbid}` | All seasons for a TMDB title |
| GET | `/api/v1/tmdb/similar/{tmdbid}/{type_name}` | Similar movies/TV shows |
| GET | `/api/v1/tmdb/recommend/{tmdbid}/{type_name}` | Recommended movies/TV shows |
| GET | `/api/v1/tmdb/collection/{collection_id}` | Collection details. Params: `page`, `count` |
| GET | `/api/v1/tmdb/credits/{tmdbid}/{type_name}` | Cast and crew. Params: `page` |
| GET | `/api/v1/tmdb/person/{person_id}` | Person details |
| GET | `/api/v1/tmdb/person/credits/{person_id}` | Person's filmography. Params: `page` |
| GET | `/api/v1/tmdb/{tmdbid}/{season}` | All episodes of a season. Params: `episode_group` |

### Douban (5 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/douban/{doubanid}` | Douban media detail |
| GET | `/api/v1/douban/person/{person_id}` | Person detail |
| GET | `/api/v1/douban/person/credits/{person_id}` | Person filmography. Params: `page` |
| GET | `/api/v1/douban/credits/{doubanid}/{type_name}` | Cast info (type_name: movie/tv) |
| GET | `/api/v1/douban/recommend/{doubanid}/{type_name}` | Recommendations |

### Bangumi (5 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/bangumi/{bangumiid}` | Bangumi detail |
| GET | `/api/v1/bangumi/credits/{bangumiid}` | Cast. Params: `page`, `count` |
| GET | `/api/v1/bangumi/recommend/{bangumiid}` | Recommendations. Params: `page`, `count` |
| GET | `/api/v1/bangumi/person/{person_id}` | Person detail |
| GET | `/api/v1/bangumi/person/credits/{person_id}` | Person filmography. Params: `page`, `count` |

### Search / Torrents / Subtitles (11 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/search/media/{mediaid}` | Search torrents by media ID (format: `tmdb:123` / `douban:123` / `bangumi:123`). Params: `mtype`, `area`, `title`, `year`, `season`, `sites` |
| GET | `/api/v1/search/media/{mediaid}/stream` | Stream torrent search by media ID with SSE. Params: `mtype`, `area`, `title`, `year`, `season`, `sites` |
| GET | `/api/v1/search/title` | Fuzzy search torrents by keyword. Params: `keyword`, `page`, `sites` |
| GET | `/api/v1/search/title/stream` | Stream fuzzy torrent search with SSE. Params: `keyword`, `page`, `sites` |
| GET | `/api/v1/search/subtitle/title` | Fuzzy search site subtitles by keyword. Params: `keyword`, `page`, `sites` |
| GET | `/api/v1/search/subtitle/title/stream` | Stream fuzzy site subtitle search with SSE. Params: `keyword`, `page`, `sites` |
| GET | `/api/v1/search/subtitle/media/{mediaid}` | Exact subtitle search by media ID (format: `tmdb:123` / `douban:123` / `bangumi:123`). Params: `mtype`, `title`, `year`, `season`, `episode`, `sites` |
| GET | `/api/v1/search/subtitle/media/{mediaid}/stream` | Stream exact subtitle search by media ID with SSE. Params: `mtype`, `title`, `year`, `season`, `episode`, `sites` |
| GET | `/api/v1/search/last` | Get latest search results |
| GET | `/api/v1/search/last/context` | Get latest search results with replayable params. `params.result_type` is `torrent` or `subtitle` |
| POST | `/api/v1/search/recommend` | AI recommended resources. Body: `filtered_indices`, `check_only`, `force` |

### Download (8 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/download/` | List active downloads. Params: `name` (downloader name) |
| POST | `/api/v1/download/` | Add download (with media info). Body: JSON |
| POST | `/api/v1/download/add` | Add download (without media info). Body: JSON with `torrent_url` |
| POST | `/api/v1/download/subtitle` | Download subtitle file to the recognized media download directory. Body: `subtitle_in`, optional `tmdbid`, `doubanid`, `save_path` |
| GET | `/api/v1/download/start/{hashString}` | Resume download task |
| GET | `/api/v1/download/stop/{hashString}` | Pause download task |
| GET | `/api/v1/download/clients` | List available download clients |
| DELETE | `/api/v1/download/{hashString}` | Delete download task. Params: `name` |

### Subscribe (28 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/subscribe/` | List all subscriptions |
| POST | `/api/v1/subscribe/` | Add subscription. Body: Subscribe JSON |
| PUT | `/api/v1/subscribe/` | Update subscription. Body: Subscribe JSON |
| GET | `/api/v1/subscribe/list` | List subscriptions (API_TOKEN auth, use `--token-param`) |
| GET | `/api/v1/subscribe/{subscribe_id}` | Subscription detail |
| DELETE | `/api/v1/subscribe/{subscribe_id}` | Delete subscription |
| PUT | `/api/v1/subscribe/status/{subid}` | Update subscription status. Params: `state` (required) |
| GET | `/api/v1/subscribe/media/{mediaid}` | Query subscription by media ID. Params: `season`, `title` |
| DELETE | `/api/v1/subscribe/media/{mediaid}` | Delete subscription by media ID. Params: `season` |
| GET | `/api/v1/subscribe/refresh` | Refresh all subscriptions |
| GET | `/api/v1/subscribe/reset/{subid}` | Reset subscription |
| GET | `/api/v1/subscribe/check` | Refresh subscription TMDB info |
| GET | `/api/v1/subscribe/search` | Search all subscriptions |
| GET | `/api/v1/subscribe/search/{subscribe_id}` | Search specific subscription |
| POST | `/api/v1/subscribe/seerr` | Overseerr/Jellyseerr notification subscription |
| GET | `/api/v1/subscribe/history/{mtype}` | Subscription history. Params: `page`, `count` |
| DELETE | `/api/v1/subscribe/history/{history_id}` | Delete subscription history |
| GET | `/api/v1/subscribe/popular` | Popular subscriptions. Params: `stype` (required), `page`, `count`, `min_sub`, `genre_id`, `min_rating`, `max_rating`, `sort_type` |
| GET | `/api/v1/subscribe/user/{username}` | User's subscriptions |
| GET | `/api/v1/subscribe/files/{subscribe_id}` | Subscription related files |
| POST | `/api/v1/subscribe/share` | Share subscription. Body: SubscribeShare JSON |
| DELETE | `/api/v1/subscribe/share/{share_id}` | Delete shared subscription |
| POST | `/api/v1/subscribe/fork` | Fork shared subscription. Body: SubscribeShare JSON |
| GET | `/api/v1/subscribe/follow` | List followed share users |
| POST | `/api/v1/subscribe/follow` | Follow a share user. Params: `share_uid` |
| DELETE | `/api/v1/subscribe/follow` | Unfollow a share user. Params: `share_uid` |
| GET | `/api/v1/subscribe/shares` | List shared subscriptions. Params: `name`, `page`, `count`, `genre_id`, `min_rating`, `max_rating`, `sort_type` |
| GET | `/api/v1/subscribe/share/statistics` | Share statistics |

### Site (25 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/site/` | List all sites |
| POST | `/api/v1/site/` | Add site. Body: Site JSON |
| PUT | `/api/v1/site/` | Update site. Body: Site JSON |
| GET | `/api/v1/site/{site_id}` | Site detail by ID |
| DELETE | `/api/v1/site/{site_id}` | Delete site |
| GET | `/api/v1/site/domain/{site_url}` | Site detail by domain |
| GET | `/api/v1/site/cookiecloud` | Sync CookieCloud |
| GET | `/api/v1/site/reset` | Reset sites |
| POST | `/api/v1/site/priorities` | Batch update site priorities. Body: array |
| POST | `/api/v1/site/cookie/{site_id}` | Update site cookie & UA. Body: `SiteCookieUpdate` JSON |
| GET | `/api/v1/site/cookie/{site_id}` | Legacy update site cookie & UA. Params: `username`, `password`, `code` |
| POST | `/api/v1/site/userdata/{site_id}` | Refresh site user data |
| GET | `/api/v1/site/userdata/{site_id}` | Get site user data. Params: `workdate` |
| GET | `/api/v1/site/userdata/latest` | All sites latest user data |
| GET | `/api/v1/site/test/{site_id}` | Test site connection |
| GET | `/api/v1/site/icon/{site_id}` | Site icon |
| GET | `/api/v1/site/category/{site_id}` | Site categories |
| GET | `/api/v1/site/resource/{site_id}` | Site resources. Params: `keyword`, `cat`, `page` |
| GET | `/api/v1/site/statistic/{site_url}` | Specific site statistics |
| GET | `/api/v1/site/statistic` | All site statistics |
| GET | `/api/v1/site/rss` | RSS subscription sites |
| GET | `/api/v1/site/auth` | Check authenticated sites |
| POST | `/api/v1/site/auth` | Authenticate a site. Body: SiteAuth |
| GET | `/api/v1/site/mapping` | Site domain-to-name mapping |
| GET | `/api/v1/site/supporting` | Supported site list |

### History (5 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/history/download` | Download history. Params: `page`, `count` |
| DELETE | `/api/v1/history/download` | Delete download history. Body: DownloadHistory JSON |
| GET | `/api/v1/history/transfer` | Transfer history. Params: `title`, `page`, `count`, `status` |
| DELETE | `/api/v1/history/transfer` | Delete transfer history. Params: `deletesrc`, `deletedest`. Body: TransferHistory |
| GET | `/api/v1/history/empty/transfer` | Clear all transfer history |

### Media Server (8 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/mediaserver/play/{itemid}` | Play media online |
| GET | `/api/v1/mediaserver/exists` | Check if media exists in library. Params: `title`, `year`, `mtype`, `tmdbid`, `season` |
| POST | `/api/v1/mediaserver/exists_remote` | Check existing episodes (remote). Body: MediaInfo JSON |
| POST | `/api/v1/mediaserver/notexists` | Check missing episodes (remote). Body: MediaInfo JSON |
| GET | `/api/v1/mediaserver/latest` | Latest library items. Params: `server` (required), `count` |
| GET | `/api/v1/mediaserver/playing` | Currently playing. Params: `server` (required), `count` |
| GET | `/api/v1/mediaserver/library` | Library list. Params: `server` (required), `hidden` |
| GET | `/api/v1/mediaserver/clients` | Available media servers |

### Storage / Files (13 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/storage/list` | List directory contents. Params: `sort`. Body: FileItem JSON |
| POST | `/api/v1/storage/mkdir` | Create directory. Params: `name` (required). Body: FileItem |
| POST | `/api/v1/storage/delete` | Delete file or directory. Body: FileItem JSON |
| POST | `/api/v1/storage/download` | Download file. Body: FileItem JSON |
| POST | `/api/v1/storage/image` | Preview image. Body: FileItem JSON |
| POST | `/api/v1/storage/rename` | Rename file/dir. Params: `new_name` (required), `recursive`. Body: FileItem |
| GET | `/api/v1/storage/usage/{name}` | Storage usage info |
| GET | `/api/v1/storage/transtype/{name}` | Supported transfer types |
| GET | `/api/v1/storage/qrcode/{name}` | Generate QR code for auth |
| GET | `/api/v1/storage/auth_url/{name}` | Get OAuth2 auth URL |
| GET | `/api/v1/storage/check/{name}` | Confirm QR login. Params: `ck`, `t` |
| POST | `/api/v1/storage/save/{name}` | Save storage config. Body: JSON object |
| GET | `/api/v1/storage/reset/{name}` | Reset storage config |

### Transfer (6 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/transfer/name` | Preview transfer name. Params: `path` (required), `filetype` (required) |
| GET | `/api/v1/transfer/queue` | Transfer queue |
| DELETE | `/api/v1/transfer/queue` | Remove from transfer queue. Body: FileItem JSON |
| POST | `/api/v1/transfer/manual/target-path` | Match manual transfer target path. Body: ManualTransferItem JSON |
| POST | `/api/v1/transfer/manual` | Manual transfer. Params: `background`. Body: ManualTransferItem JSON |
| GET | `/api/v1/transfer/now` | Run immediate transfer |

### Dashboard (19 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/dashboard/statistic` | Media statistics. Params: `name` |
| GET | `/api/v1/dashboard/statistic2` | Media statistics (API_TOKEN, use `--token-param`) |
| GET | `/api/v1/dashboard/storage` | Local storage space |
| GET | `/api/v1/dashboard/storage2` | Local storage space (API_TOKEN) |
| GET | `/api/v1/dashboard/processes` | Process info |
| GET | `/api/v1/dashboard/system` | Host name, operating system, MoviePilot runtime, and backend version |
| GET | `/api/v1/dashboard/downloader` | Downloader info. Params: `name` |
| GET | `/api/v1/dashboard/downloader2` | Downloader info (API_TOKEN) |
| GET | `/api/v1/dashboard/schedule` | Scheduled services |
| GET | `/api/v1/dashboard/schedule2` | Scheduled services (API_TOKEN) |
| GET | `/api/v1/dashboard/schedule/{job_id}/progress` | Scheduled service real-time progress |
| GET | `/api/v1/dashboard/schedule2/{job_id}/progress` | Scheduled service real-time progress (API_TOKEN) |
| GET | `/api/v1/dashboard/transfer` | Transfer statistics. Params: `days` |
| GET | `/api/v1/dashboard/cpu` | CPU usage |
| GET | `/api/v1/dashboard/cpu2` | CPU usage (API_TOKEN) |
| GET | `/api/v1/dashboard/memory` | Memory usage |
| GET | `/api/v1/dashboard/memory2` | Memory usage (API_TOKEN) |
| GET | `/api/v1/dashboard/network` | Network traffic |
| GET | `/api/v1/dashboard/network2` | Network traffic (API_TOKEN) |

### Plugin (22 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/plugin/` | List plugins. Params: `state` (installed/market/all), `force` |
| GET | `/api/v1/plugin/installed` | List installed plugins |
| GET | `/api/v1/plugin/statistic` | Plugin install statistics |
| GET | `/api/v1/plugin/install/{plugin_id}` | Install plugin. Params: `repo_url`, `force` |
| GET | `/api/v1/plugin/reload/{plugin_id}` | Reload plugin |
| GET | `/api/v1/plugin/reset/{plugin_id}` | Reset plugin config & data |
| GET | `/api/v1/plugin/{plugin_id}` | Get plugin config |
| PUT | `/api/v1/plugin/{plugin_id}` | Update plugin config. Body: JSON object |
| DELETE | `/api/v1/plugin/{plugin_id}` | Uninstall plugin |
| POST | `/api/v1/plugin/clone/{plugin_id}` | Clone plugin. Body: JSON object |
| GET | `/api/v1/plugin/form/{plugin_id}` | Plugin form page |
| GET | `/api/v1/plugin/page/{plugin_id}` | Plugin data page |
| GET | `/api/v1/plugin/remotes` | Plugin federation list. Params: `token` (required) |
| GET | `/api/v1/plugin/dashboard/meta` | All plugin dashboard metadata |
| GET | `/api/v1/plugin/dashboard/{plugin_id}/{key}` | Plugin dashboard by key |
| GET | `/api/v1/plugin/dashboard/{plugin_id}` | Plugin dashboard |
| GET | `/api/v1/plugin/file/{plugin_id}/{filepath}` | Plugin static file |
| GET | `/api/v1/plugin/folders` | Plugin folder config |
| POST | `/api/v1/plugin/folders` | Save plugin folder config |
| POST | `/api/v1/plugin/folders/{folder_name}` | Create plugin folder |
| DELETE | `/api/v1/plugin/folders/{folder_name}` | Delete plugin folder |
| PUT | `/api/v1/plugin/folders/{folder_name}/plugins` | Update folder plugins. Body: array |

### Workflow (16 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/workflow/` | List all workflows |
| POST | `/api/v1/workflow/` | Create workflow. Body: Workflow JSON |
| GET | `/api/v1/workflow/{workflow_id}` | Workflow detail |
| PUT | `/api/v1/workflow/{workflow_id}` | Update workflow. Body: Workflow JSON |
| DELETE | `/api/v1/workflow/{workflow_id}` | Delete workflow |
| POST | `/api/v1/workflow/{workflow_id}/run` | Run workflow. Params: `from_begin` |
| POST | `/api/v1/workflow/{workflow_id}/start` | Enable workflow |
| POST | `/api/v1/workflow/{workflow_id}/pause` | Disable workflow |
| POST | `/api/v1/workflow/{workflow_id}/reset` | Reset workflow |
| GET | `/api/v1/workflow/actions` | List all actions |
| GET | `/api/v1/workflow/plugin/actions` | Plugin actions. Params: `plugin_id` |
| GET | `/api/v1/workflow/event_types` | List event types |
| POST | `/api/v1/workflow/share` | Share workflow. Body: WorkflowShare JSON |
| DELETE | `/api/v1/workflow/share/{share_id}` | Delete shared workflow |
| POST | `/api/v1/workflow/fork` | Fork shared workflow. Body: WorkflowShare JSON |
| GET | `/api/v1/workflow/shares` | List shared workflows. Params: `name`, `page`, `count` |

### System (24 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/system/env` | Get system configuration, including runtime versions and Rust acceleration availability/enabled status |
| POST | `/api/v1/system/env` | Update system configuration. Body: JSON object |
| GET | `/api/v1/system/ping` | Check service availability for authenticated users |
| GET | `/api/v1/system/setting/public/{key}` | Get allowlisted non-sensitive system setting for authenticated users |
| GET | `/api/v1/system/setting/{key}` | Get system setting |
| POST | `/api/v1/system/setting/{key}` | Update system setting |
| POST | `/api/v1/system/setting/PLUGIN_MARKET/sync-wiki` | Sync plugin market repository URLs from the MoviePilot Wiki and merge with local `PLUGIN_MARKET` |
| GET | `/api/v1/system/global` | Non-sensitive settings. Params: `token` (required) |
| GET | `/api/v1/system/global/user` | User-related settings |
| GET | `/api/v1/system/restart` | Restart system |
| POST | `/api/v1/system/upgrade` | Upgrade and restart system. Body: `"release"` or `"dev"` |
| GET | `/api/v1/system/runscheduler` | Run scheduled service. Params: `jobid` (required) |
| GET | `/api/v1/system/runscheduler2` | Run scheduler (API_TOKEN, use `--token-param`). Params: `jobid` |
| GET | `/api/v1/system/modulelist` | List loaded modules |
| GET | `/api/v1/system/moduletest/{moduleid}` | Test module availability |
| GET | `/api/v1/system/versions` | List all GitHub releases |
| GET | `/api/v1/system/ruletest` | Test filter rule. Params: `title` (required), `rulegroup_name` (required), `subtitle` |
| GET | `/api/v1/system/nettest` | Test network connectivity. Params: `url` (required), `proxy` (required), `include` |
| GET | `/api/v1/system/llm-models` | List LLM models. Params: `provider` (required), `api_key` (required), `base_url` |
| GET | `/api/v1/system/progress/{process_type}` | Real-time progress (SSE) |
| GET | `/api/v1/system/message` | Real-time messages (SSE). Params: `role` |
| GET | `/api/v1/system/logging` | Real-time logs (SSE). Params: `length`, `logfile` |
| GET | `/api/v1/system/img/{proxy}` | Image proxy. Params: `imgurl` (required), `cache`, `use_cookies` |
| GET | `/api/v1/system/cache/image` | Cached image. Params: `url` (required) |

### Discover (6 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/discover/source` | Discover data sources |
| GET | `/api/v1/discover/bangumi` | Discover Bangumi. Params: `type`, `cat`, `sort`, `year`, `page`, `count` |
| GET | `/api/v1/discover/douban_movies` | Discover Douban movies. Params: `sort`, `tags`, `page`, `count` |
| GET | `/api/v1/discover/douban_tvs` | Discover Douban TV. Params: `sort`, `tags`, `page`, `count` |
| GET | `/api/v1/discover/tmdb_movies` | Discover TMDB movies. Params: `sort_by`, `with_genres`, `with_original_language`, `page` |
| GET | `/api/v1/discover/tmdb_tvs` | Discover TMDB TV. Params: same as movies |

### Recommend (14 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/recommend/source` | Recommendation data sources |
| GET | `/api/v1/recommend/bangumi_calendar` | Bangumi daily schedule. Params: `page`, `count` |
| GET | `/api/v1/recommend/douban_showing` | Douban now showing. Params: `page`, `count` |
| GET | `/api/v1/recommend/douban_movies` | Douban movies. Params: `sort`, `tags`, `page`, `count` |
| GET | `/api/v1/recommend/douban_tvs` | Douban TV. Params: `sort`, `tags`, `page`, `count` |
| GET | `/api/v1/recommend/douban_movie_top250` | Douban Top 250 movies. Params: `page`, `count` |
| GET | `/api/v1/recommend/douban_tv_weekly_chinese` | Douban Chinese TV weekly. Params: `page`, `count` |
| GET | `/api/v1/recommend/douban_tv_weekly_global` | Douban Global TV weekly. Params: `page`, `count` |
| GET | `/api/v1/recommend/douban_tv_animation` | Douban animation. Params: `page`, `count` |
| GET | `/api/v1/recommend/douban_movie_hot` | Douban hot movies. Params: `page`, `count` |
| GET | `/api/v1/recommend/douban_tv_hot` | Douban hot TV. Params: `page`, `count` |
| GET | `/api/v1/recommend/tmdb_movies` | TMDB movies. Params: `sort_by`, `with_genres`, `page` |
| GET | `/api/v1/recommend/tmdb_tvs` | TMDB TV. Params: `sort_by`, `with_genres`, `page` |
| GET | `/api/v1/recommend/tmdb_trending` | TMDB trending. Params: `page` |

### Torrent Cache (5 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/torrent/cache` | Get torrent cache |
| DELETE | `/api/v1/torrent/cache` | Clear torrent cache |
| DELETE | `/api/v1/torrent/cache/{domain}/{torrent_hash}` | Delete specific torrent cache |
| POST | `/api/v1/torrent/cache/refresh` | Refresh torrent cache |
| POST | `/api/v1/torrent/cache/reidentify/{domain}/{torrent_hash}` | Re-identify torrent. Params: `tmdbid`, `doubanid` |

### Message (8 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/message/` | Receive user message. Params: `token`, `source` |
| GET | `/api/v1/message/` | Callback verification. Params: `token`, `echostr`, `msg_signature`, `timestamp`, `nonce`, `source` |
| POST | `/api/v1/message/web` | Send web message. Params: `text` (required) |
| GET | `/api/v1/message/web` | Get web messages. Params: `page`, `count` |
| GET | `/api/v1/message/notification` | Get notification history. Params: `page`, `count`; server filters cleared history |
| DELETE | `/api/v1/message/notification` | Mark notification history as cleared. Params: `scope` (`all`, `system`, `media`) |
| POST | `/api/v1/message/webpush/subscribe` | WebPush subscribe. Body: Subscription JSON |
| POST | `/api/v1/message/webpush/send` | Send WebPush notification. Body: SubscriptionMessage JSON |

### User (10 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/user/` | List all users |
| POST | `/api/v1/user/` | Create user. Body: UserCreate JSON |
| PUT | `/api/v1/user/` | Update user. Body: UserUpdate JSON |
| GET | `/api/v1/user/current` | Current logged-in user |
| GET | `/api/v1/user/{username}` | User detail |
| DELETE | `/api/v1/user/id/{user_id}` | Delete user by ID |
| DELETE | `/api/v1/user/name/{user_name}` | Delete user by username |
| POST | `/api/v1/user/avatar/{user_id}` | Upload avatar. Body: multipart/form-data |
| GET | `/api/v1/user/config/{key}` | Get user config |
| POST | `/api/v1/user/config/{key}` | Update user config |

### Login (3 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/login/access-token` | Get JWT access token. Body: form (username, password) |
| GET | `/api/v1/login/wallpaper` | Login page wallpaper |
| GET | `/api/v1/login/wallpapers` | Login page wallpaper list |

### MCP Tools (6 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/mcp` | MCP JSON-RPC 2.0 endpoint |
| DELETE | `/api/v1/mcp` | Terminate MCP session |
| GET | `/api/v1/mcp/tools` | List all exposed tools |
| POST | `/api/v1/mcp/tools/call` | Call a tool. Body: `{"tool_name":"...","arguments":{...}}` |
| GET | `/api/v1/mcp/tools/{tool_name}` | Get tool definition |
| GET | `/api/v1/mcp/tools/{tool_name}/schema` | Get tool input schema |

### Agent MCP Client (3 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/message/agent/mcp/servers` | List external MCP servers configured for the built-in Agent. Superuser login required |
| POST | `/api/v1/message/agent/mcp/servers` | Save external MCP servers for the built-in Agent. Body: `{"servers":[...]}` |
| POST | `/api/v1/message/agent/mcp/servers/test` | Test one external MCP server and return discovered tools. Body: `{"server":{...}}` |

### Webhook (2 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/webhook/` | Webhook message (GET). Params: `token`, `source` |
| POST | `/api/v1/webhook/` | Webhook message (POST). Params: `token`, `source` |

### Servarr Compatibility -- /api/v3 (16 endpoints)

Radarr/Sonarr compatible API for integration with external tools.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v3/system/status` | System status |
| GET | `/api/v3/qualityProfile` | Quality profiles |
| GET | `/api/v3/rootfolder` | Root folders |
| GET | `/api/v3/tag` | Tags |
| GET | `/api/v3/languageprofile` | Languages |
| GET | `/api/v3/movie` | All subscribed movies |
| POST | `/api/v3/movie` | Add movie subscription. Body: RadarrMovie JSON |
| GET | `/api/v3/movie/lookup` | Search movie. Params: `term` (format: `tmdb:123`) |
| GET | `/api/v3/movie/{mid}` | Movie detail |
| DELETE | `/api/v3/movie/{mid}` | Delete movie subscription |
| GET | `/api/v3/series` | All TV series |
| POST | `/api/v3/series` | Add TV subscription. Body: SonarrSeries JSON |
| PUT | `/api/v3/series` | Update TV subscription. Body: SonarrSeries JSON |
| GET | `/api/v3/series/lookup` | Search TV. Params: `term` (format: `tvdb:123`) |
| GET | `/api/v3/series/{tid}` | TV detail |
| DELETE | `/api/v3/series/{tid}` | Delete TV subscription |

### CookieCloud -- /cookiecloud (5 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/cookiecloud/` | Root |
| POST | `/cookiecloud/` | Root |
| POST | `/cookiecloud/update` | Upload cookie data. Body: CookieData JSON |
| GET | `/cookiecloud/get/{uuid}` | Download encrypted data |
| POST | `/cookiecloud/get/{uuid}` | Download encrypted data (POST) |

---

## Common Workflows

### Search and download a movie

```bash
# 1. Search TMDB for the movie
python scripts/mp-api.py GET /api/v1/media/search title="Inception" type="movie"

# 2. Get media detail (replace {tmdbid} with actual ID)
python scripts/mp-api.py GET /api/v1/media/27205 type_name="movie"

# 3. Search torrents
python scripts/mp-api.py GET /api/v1/search/media/tmdb:27205 mtype="movie"

# 4. Get latest search results
python scripts/mp-api.py GET /api/v1/search/last

# 5. Add download
python scripts/mp-api.py POST /api/v1/download/add --json '{"torrent_url":"<url_from_search>"}'
```

### Search and download subtitles

```bash
# 1. Search site subtitles by keyword
python scripts/mp-api.py GET /api/v1/search/subtitle/title keyword="Inception" sites="1,2"

# 2. Restore the last subtitle search with replayable params
python scripts/mp-api.py GET /api/v1/search/last/context

# 3. Download a subtitle result to the recognized media directory
python scripts/mp-api.py POST /api/v1/download/subtitle --json '{"subtitle_in":{"title":"Inception.2010.1080p.chs","enclosure":"https://example.com/downloadsubs.php?torrentid=1&subid=2","site_name":"Example"},"tmdbid":27205}'
```

### Add a subscription

```bash
# 1. Search for the show
python scripts/mp-api.py GET /api/v1/media/search title="Breaking Bad" type="tv"

# 2. Check if already subscribed
python scripts/mp-api.py GET /api/v1/subscribe/media/tmdb:1396

# 3. Check if already in library
python scripts/mp-api.py GET /api/v1/mediaserver/exists tmdbid=1396 mtype="tv"

# 4. Add subscription
python scripts/mp-api.py POST /api/v1/subscribe/ --json '{"name":"Breaking Bad","year":"2008","type":"tv","tmdbid":1396}'
```

### System monitoring

```bash
# CPU, memory, network
python scripts/mp-api.py GET /api/v1/dashboard/cpu
python scripts/mp-api.py GET /api/v1/dashboard/memory
python scripts/mp-api.py GET /api/v1/dashboard/network

# Storage
python scripts/mp-api.py GET /api/v1/dashboard/storage

# Active downloads
python scripts/mp-api.py GET /api/v1/download/

# Run a scheduled task
python scripts/mp-api.py GET /api/v1/system/runscheduler jobid="subscribe_search_all"
```

### Site management

```bash
# List all sites
python scripts/mp-api.py GET /api/v1/site/

# Test site connectivity
python scripts/mp-api.py GET /api/v1/site/test/1

# Get site user data
python scripts/mp-api.py GET /api/v1/site/userdata/1

# Sync CookieCloud
python scripts/mp-api.py GET /api/v1/site/cookiecloud
```

## Error Handling

| Scenario | Action |
|----------|--------|
| HTTP 401 | API key is invalid or missing. Verify local settings with `moviepilot doctor`; only use `--apikey` as an external fallback. |
| HTTP 403 | Insufficient permissions. The API key grants superuser access; check if the endpoint requires special auth. |
| HTTP 404 | Endpoint or resource not found. Verify the path and path parameters. |
| HTTP 422 | Validation error. Check required parameters and JSON body format. |
| Connection error | Verify `--host` URL is reachable. Check if MoviePilot is running. |
| Missing config | Run inside the MoviePilot project, or set `MP_HOST` and `MP_API_KEY` in the process environment. |
