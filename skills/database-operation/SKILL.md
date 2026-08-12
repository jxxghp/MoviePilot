---
name: database-operation
version: 4
description: >-
  Use this skill when you need to inspect, query, maintain, or carefully modify
  the MoviePilot database. This skill uses the bundled scripts/mp-db.py helper,
  which reads MoviePilot local settings itself and never requires database
  passwords or full PostgreSQL DSNs in the agent prompt. Applicable scenarios
  include data statistics, counts, aggregations, inspecting or fixing records,
  cleanup requests, and questions like "how many downloads", "show site stats",
  "delete old records", or "why is this subscription stuck".
---

# Database Operation

> All script paths are relative to this skill file.

Use `scripts/mp-db.py` for all database access. Do not extract database passwords, API tokens, or full PostgreSQL DSNs from the prompt. The script reads MoviePilot local settings and connects to SQLite or PostgreSQL internally.

## Scope And Boundaries

This skill is the direct SQL boundary. It is implemented as a Python script and
is appropriate when the agent must inspect records, run data statistics, repair
stuck state, or perform an explicitly requested database update.

Prefer safer product surfaces first:

| Request | Preferred skill |
|---|---|
| Normal local MoviePilot product operation exposed as an MCP tool | `moviepilot-cli` |
| Direct REST endpoint call | `moviepilot-api` |
| Slash commands or plugin/system command dispatch | `command-dispatch` |
| Manual file organization | `organize-files` |
| Retry failed transfer history records | `transfer-failed-retry` |

Use this skill as the final fallback for data access or mutation. It may run
`SELECT`, `INSERT`, `UPDATE`, `DELETE`, and schema-changing statements through
the bundled script, but broad or destructive writes still require explicit user
authorization.

## Commands

List tables:

```bash
python scripts/mp-db.py tables
```

Show table schema:

```bash
python scripts/mp-db.py schema downloadhistory
```

Run a read query:

```bash
python scripts/mp-db.py query "SELECT COUNT(*) AS total FROM downloadhistory"
```

Read SQL from stdin or a file:

```bash
python scripts/mp-db.py query --file /path/to/query.sql
```

Run a write statement:

```bash
python scripts/mp-db.py write "UPDATE subscribe SET state = 'S' WHERE id = 123"
```

`query --write` is also supported for compatibility, but prefer the `write` subcommand for `INSERT`, `UPDATE`, `DELETE`, and schema changes.

## Workflow

1. Prefer existing MoviePilot tools or APIs for normal product workflows.
2. Use this skill for direct database inspection only when no existing tool covers the request.
3. For unknown schema, run `tables` first, then `schema <table>`.
4. For `SELECT` queries, execute directly with a narrow projection and an explicit `LIMIT` when reading rows.
5. For `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE`, `CREATE`, or `REPLACE`, use `write` and report the affected row count.

## Built-in Safety

- `query` defaults to read-only mode.
- `write` executes data updates and schema-changing statements directly.
- `query --write` remains available as a compatibility alias for write statements.
- Multiple SQL statements in one invocation are rejected.
- Plain `SELECT` queries get a default `LIMIT 100` if no limit is present.
- Query results are returned exactly as stored. The agent may use sensitive values internally when needed, but must not echo secrets in the final user-facing response unless the user explicitly asks to inspect that value.

## Safety Rules

1. Confirm before destructive or broad write operations when the user has not already clearly authorized the exact change.
2. Suggest a backup before destructive operations such as `DELETE`, `DROP`, or `TRUNCATE`.
3. Never run `UPDATE` or `DELETE` without a `WHERE` clause unless the user explicitly intends to affect all rows.
4. Raw secrets, cookies, passkeys, hashed passwords, OTP secrets, API keys, or tokens may appear in tool output. Use them only for the requested operation and avoid repeating them in the final response unless explicitly requested.
5. Keep output small. Summarize large results instead of dumping them.

## Core Tables

### downloadhistory
Key columns: `id`, `path`, `type`, `title`, `year`, `media_source`, `media_id`, `music_type`, `seasons`, `episodes`, `downloader`, `download_hash`, `torrent_name`, `torrent_site`, `userid`, `username`, `date`, `media_category`

### downloadfiles
Key columns: `id`, `downloader`, `download_hash`, `fullpath`, `savepath`, `filepath`, `torrentname`, `state`

### transferhistory

Music rows persist actual `audio_format`, `audio_lossless`, `bit_depth`, `sample_rate`, and `bitrate` values read during organization. Bitrate uses bps and sample rate uses Hz.
Key columns: `id`, `src`, `dest`, `mode`, `type`, `category`, `title`, `year`, `media_source`, `media_id`, `music_type`, `seasons`, `episodes`, `download_hash`, `status`, `errmsg`, `date`

### downloadfailure

Key columns: `id`, `fingerprint`, `type`, `title`, `year`, `media_source`, `media_id`, `seasons`, `episodes`, `site`, `torrent_id`, `downloader`, `error_message`, `retry_count`, `next_retry_at`

### subscribe

Music filters use `audio_quality`, `audio_format`, `min_bitrate`, `min_bit_depth`, and `min_sample_rate`. Quality upgrades reuse `current_priority` and persist the current exact values in `current_audio_format`, `current_bitrate`, `current_bit_depth`, and `current_sample_rate`.
Key columns: `id`, `name`, `year`, `type`, `media_source`, `media_id`, `music_type`, `season`, `total_episode`, `start_episode`, `lack_episode`, `state`, `filter`, `include`, `exclude`, `quality`, `resolution`, `sites`, `best_version`, `best_version_full`, `date`, `username`

### subscribehistory

Completed music subscriptions retain both audio filters and the final current-quality snapshot for auditing.
Key columns: `id`, `name`, `year`, `type`, `media_source`, `media_id`, `music_type`, `season`, `total_episode`, `start_episode`, `date`, `username`

### user
Key columns: `id`, `name`, `email`, `is_active`, `is_superuser`, `permissions`, `settings`

### site
Key columns: `id`, `name`, `domain`, `url`, `pri`, `cookie`, `proxy`, `is_active`, `downloader`, `limit_interval`, `limit_count`

### siteuserdata
Key columns: `id`, `domain`, `name`, `username`, `user_level`, `bonus`, `upload`, `download`, `ratio`, `seeding`, `leeching`, `seeding_size`, `updated_day`

### sitestatistic
Key columns: `id`, `domain`, `success`, `fail`, `seconds`, `lst_state`, `lst_mod_date`

### mediaserveritem
Key columns: `id`, `server`, `library`, `item_id`, `item_type`, `title`, `original_title`, `year`, `media_source`, `media_id`, `path`

The media-bearing tables above store one primary identity only. Treat
`media_source` and `media_id` as an atomic pair: both are null for an unknown
identity, or both contain a valid source enum value and its native ID. Do not
write source-specific identity columns back into these tables.

### systemconfig
Key columns: `id`, `key`, `value`

### userconfig
Key columns: `id`, `username`, `key`, `value`

### plugindata
Key columns: `id`, `plugin_id`, `key`, `value`

### message
Key columns: `id`, `channel`, `source`, `mtype`, `title`, `text`, `image`, `link`, `userid`, `reg_time`

### workflow
Key columns: `id`, `name`, `description`, `timer`, `trigger_type`, `event_type`, `state`, `run_count`, `actions`, `flows`, `last_time`

### passkey
Key columns: `id`, `user_id`, `credential_id`, `public_key`, `name`, `created_at`, `last_used_at`, `is_active`

### siteicon
Key columns: `id`, `name`, `domain`, `url`, `base64`

## Common Queries

Total downloads:

```sql
SELECT COUNT(*) AS total FROM downloadhistory
```

Recent download history:

```sql
SELECT title, year, type, torrent_site, date FROM downloadhistory ORDER BY id DESC LIMIT 10
```

Failed transfers:

```sql
SELECT id, title, src, errmsg, date FROM transferhistory WHERE status = 0 ORDER BY id DESC LIMIT 10
```

Active subscriptions:

```sql
SELECT name, year, type, season, state, lack_episode FROM subscribe WHERE state = 'R' LIMIT 50
```

Site upload/download statistics:

```sql
SELECT name, domain, upload, download, ratio, bonus, seeding, user_level FROM siteuserdata ORDER BY upload DESC LIMIT 50
```

Media library statistics:

```sql
SELECT server, library, COUNT(*) AS count FROM mediaserveritem GROUP BY server, library
```

Site access success rate:

```sql
SELECT domain, success, fail, ROUND(success * 100.0 / (success + fail), 1) AS success_rate FROM sitestatistic WHERE success + fail > 0 ORDER BY success_rate DESC LIMIT 50
```

Plugin data keys:

```sql
SELECT plugin_id, key FROM plugindata ORDER BY plugin_id, key LIMIT 100
```

## SQL Dialect Notes

| Feature | SQLite | PostgreSQL |
|---|---|---|
| Boolean values | `0` / `1` | `false` / `true` |
| String concat | `||` | `||` or `CONCAT()` |
| Current time | `datetime('now')` | `NOW()` |
| JSON access | `json_extract(col, '$.key')` | `col->>'key'` |
| Case-insensitive match | `LIKE` | `ILIKE` |

## Troubleshooting

- Missing dependency: run inside the MoviePilot project environment so SQLAlchemy and database drivers are available.
- Connection failure: verify MoviePilot config with `moviepilot doctor`.
- Table not found: run `python scripts/mp-db.py tables`, then inspect the table with `schema`.
