---
name: database-operation
version: 6
description: >-
  Use this skill when you need to inspect, query, maintain, or carefully modify
  the MoviePilot database. This skill uses the bundled scripts/mp-db.py helper,
  which reads MoviePilot local settings itself and never requires database
  passwords or full PostgreSQL DSNs in the agent prompt. Applicable scenarios
  include data statistics, counts, aggregations, inspecting or fixing records,
  cleanup requests, and questions like "how many downloads", "show site stats",
  "delete old records", or "why is this subscription stuck".
allowed-tools: execute_command
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
| Normal MoviePilot product operation | `moviepilot-api` structured operations |
| Operation outside the structured API catalog | A more specific Skill or explicit unsupported result |
| Slash commands or plugin/system command dispatch | `command-dispatch` |
| Manual file organization | `organize-files` |
| Retry failed transfer history records | `transfer-failed-retry` |

Use this skill as the final fallback for data access or mutation. It may run
`SELECT`, `INSERT`, `UPDATE`, `DELETE`, and schema-changing statements through
the bundled script, but broad or destructive writes still require explicit user
authorization.

System settings have two managed sources and should not normally be edited here:

- Runtime `Settings` variables are queried and updated by `moviepilot-api`
  operations `config.system.get` / `config.system.update`; updates perform type
  conversion and persist to `app.env`.
- `SystemConfigKey` values are stored in the database `systemconfig` table, but
  the same API operations must be preferred because they enforce registered
  keys, plugin mutation admission, value normalization, secret redaction, and
  configuration-change events.

Use direct SQL against `systemconfig` only for an explicitly authorized repair
when the managed API cannot complete the operation. Inspect the exact row first,
avoid broad writes, and verify the managed API can read the repaired value.

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

`tables` returns the tables that exist in the current instance. The catalog below covers every MoviePilot ORM table plus Alembic metadata. Always treat the live `schema <table>` result as authoritative.

### `agentchat`
- Purpose: Stores Web Agent and messaging-channel session indexes, titles, previews, and message snapshots.
- Useful queries: Tracing Agent history or context restoration by user, session, or update time.
- Write boundary: Owned by the Agent conversation service; do not rewrite message JSON, counters, or ownership.
- Columns: `id`, `session_id`, `client_session_id`, `user_id`, `username`, `channel`, `source`, `original_chat_id`, `title`, `preview`, `agent_messages`, `display_messages`, `message_count`, `created_at`, `updated_at`

### `agenttask`
- Purpose: Stores one-shot or recurring Agent task definitions, triggers, and the latest execution summary.
- Useful queries: Inspecting task ownership, enablement, cron/run_at settings, and the latest result.
- Write boundary: Create, update, enable, disable, or delete tasks through the Agent task API.
- Columns: `id`, `name`, `content`, `trigger_type`, `cron_expression`, `run_at`, `enabled`, `user_id`, `username`, `session_id`, `channel`, `source`, `original_chat_id`, `last_status`, `last_run_at`, `last_result`, `last_run_id`, `run_count`, `created_at`, `updated_at`

### `agenttaskrun`
- Purpose: Stores the input snapshot, status, timestamps, and result of each Agent task execution.
- Useful queries: Auditing one run or correlating a failure with task_id, run_id, and trigger source.
- Write boundary: Execution evidence owned by the task runner; never fabricate rows or edit run status.
- Columns: `id`, `run_id`, `task_id`, `trigger_source`, `name`, `content`, `trigger_type`, `cron_expression`, `run_at`, `user_id`, `username`, `session_id`, `channel`, `message_source`, `original_chat_id`, `status`, `started_at`, `finished_at`, `result`

### `alembic_version`
- Purpose: Records the Alembic migration revision currently applied to the database.
- Useful queries: Diagnosing startup migration failures or a database/code revision mismatch.
- Write boundary: Never edit it directly; advance or roll back revisions only through Alembic.
- Columns: `version_num`

### `downloadfailure`
- Purpose: Stores stable fingerprints, media/torrent context, errors, and retry scheduling for failed downloads.
- Useful queries: Analyzing failure causes, retry counts, next retry time, and affected media or sites.
- Write boundary: Owned by download-failure compensation; retry or clean records through its business API.
- Columns: `id`, `fingerprint`, `type`, `title`, `year`, `media_source`, `media_id`, `seasons`, `episodes`, `site`, `site_name`, `torrent_id`, `torrent_name`, `torrent_size`, `downloader`, `source`, `error_message`, `retry_count`, `first_failed_at`, `last_failed_at`, `next_retry_at`

### `downloadfiles`
- Purpose: Maps downloader task hashes to full paths, save directories, relative files, and active state.
- Useful queries: Finding task files by downloader/download_hash or diagnosing savepath associations.
- Write boundary: Maintained by download and transfer flows; do not manually change state or path mappings.
- Columns: `id`, `downloader`, `download_hash`, `fullpath`, `savepath`, `filepath`, `torrentname`, `state`

### `downloadhistory`
- Purpose: Stores media identity, torrent, downloader, user, and recognition context for submitted downloads.
- Useful queries: Reviewing download history or tracing a media identity or hash back to its source.
- Write boundary: Written by the download use case; delete or correct records through the download-history API.
- Columns: `id`, `path`, `type`, `title`, `year`, `media_source`, `media_id`, `music_type`, `seasons`, `episodes`, `image`, `poster`, `downloader`, `download_hash`, `torrent_name`, `torrent_description`, `torrent_site`, `userid`, `username`, `channel`, `date`, `note`, `media_category`, `episode_group`, `custom_words`

### `mediaserveritem`
- Purpose: Stores the local index and canonical media identity projected from media-server libraries.
- Useful queries: Checking library presence, server/library/path placement, and season information.
- Write boundary: This is a rebuildable projection; writes and cleanup belong to media-server synchronization.
- Columns: `id`, `server`, `library`, `item_id`, `item_type`, `title`, `original_title`, `year`, `media_source`, `media_id`, `path`, `seasoninfo`, `note`, `lst_mod_date`

### `message`
- Purpose: Stores inbound and outbound messages, channels, content, attachments, users, and timestamps.
- Useful queries: Paging notification history, distinguishing direction, or tracing duplicates by source.
- Write boundary: Written by messaging and notification services; clean it through the message API or retention job.
- Columns: `id`, `channel`, `source`, `mtype`, `title`, `text`, `image`, `link`, `userid`, `reg_time`, `action`, `note`

### `outboxmessage`
- Purpose: Stores externally visible side-effect intents committed atomically with business transactions.
- Useful queries: Diagnosing pending/processing/failed state, leases, attempts, and the last error.
- Write boundary: Owned by the Outbox Dispatcher state machine; never mark completion or delete undelivered events manually.
- Columns: `id`, `event_key`, `topic`, `payload_version`, `payload`, `status`, `attempt`, `next_retry_at`, `lease_until`, `last_error`, `created_at`, `completed_at`

### `passkey`
- Purpose: Stores WebAuthn/PassKey credentials, public keys, signature counters, and activation state.
- Useful queries: Authorized authentication diagnostics such as ownership, activation, and last use.
- Write boundary: Security-sensitive; manage it only through the PassKey API and never disclose credential material.
- Columns: `id`, `user_id`, `credential_id`, `public_key`, `sign_count`, `name`, `aaguid`, `created_at`, `last_used_at`, `is_active`, `transports`

### `plugindata`
- Purpose: Stores plugin-owned JSON values isolated by plugin_id and key.
- Useful queries: Diagnosing persistence or migration issues for one explicitly identified plugin and key.
- Write boundary: The plugin owns these values; prefer plugin capabilities or the plugin-data API.
- Columns: `id`, `plugin_id`, `key`, `value`

### `pluginidentity`
- Purpose: Stores trusted source, payload source, version, receipt, and CAS revision for a physical plugin package.
- Useful queries: Auditing source binding, package generation, payload application, or identity conflicts.
- Write boundary: Plugin supply-chain state owned exclusively by installation and update transactions.
- Columns: `id`, `plugin_id`, `normalized_plugin_id`, `trusted_source_type`, `trusted_source_key`, `binding_basis`, `payload_source_type`, `payload_source_key`, `declared_version`, `package_generation`, `declared_metadata`, `payload_receipt`, `revision`, `created_at`, `updated_at`, `bound_at`, `payload_applied_at`

### `plugininstallation`
- Purpose: Stores plugin installation phase, membership target, identity revisions, and backup state.
- Useful queries: Diagnosing interrupted installations, rollback conditions, and package or backup presence.
- Write boundary: Owned by the plugin installation state machine; never advance phase or overwrite evidence manually.
- Columns: `id`, `transaction_id`, `plugin_id`, `phase`, `membership_before`, `membership_target`, `identity_before_revision`, `identity_target_revision`, `package_existed`, `persistent_backup_existed`, `created_at`, `updated_at`, `schema_version`

### `site`
- Purpose: Stores private-tracker URLs, RSS, credentials, rate limits, proxy state, and downloader binding.
- Useful queries: Inspecting enablement, domain, rate limits, or downloader binding with minimal credential exposure.
- Write boundary: Contains cookies, API keys, and tokens; manage it through the site API.
- Columns: `id`, `name`, `domain`, `url`, `pri`, `rss`, `cookie`, `ua`, `apikey`, `token`, `proxy`, `filter`, `render`, `public`, `note`, `limit_interval`, `limit_count`, `limit_seconds`, `timeout`, `is_active`, `lst_mod_date`, `downloader`

### `siteicon`
- Purpose: Caches site names, domains, icon URLs, and Base64 icon content.
- Useful queries: Diagnosing missing icons, incorrect domain mapping, or cache generation.
- Write boundary: Rebuildable cache owned by site-icon synchronization; direct writes are not recommended.
- Columns: `id`, `name`, `domain`, `url`, `base64`

### `sitestatistic`
- Purpose: Aggregates site request successes, failures, durations, latest state, and diagnostic notes.
- Useful queries: Comparing site availability, failure rate, and the most recent access state.
- Write boundary: Accumulated by site access statistics; never edit counters to conceal runtime behavior.
- Columns: `id`, `domain`, `success`, `fail`, `seconds`, `lst_state`, `lst_mod_date`, `note`

### `siteuserdata`
- Purpose: Stores tracker account level, traffic, ratio, seeding, and unread-message data.
- Useful queries: Inspecting account state, traffic trends, seeding volume, and the latest collection error.
- Write boundary: A site-scraping projection refreshed by synchronization; do not edit it directly.
- Columns: `id`, `domain`, `name`, `username`, `userid`, `user_level`, `join_at`, `bonus`, `upload`, `download`, `ratio`, `seeding`, `leeching`, `seeding_size`, `leeching_size`, `seeding_info`, `message_unread`, `message_unread_contents`, `err_msg`, `updated_day`, `updated_time`

### `subscribe`
- Purpose: Stores active movie, TV, or music subscriptions, filters, progress, and download targets.
- Useful queries: Inspecting state, missing episodes/tracks, quality rules, site scope, and match progress.
- Write boundary: Create, update, search, or delete through the subscription API to preserve state-machine consistency.
- Columns: `id`, `name`, `year`, `type`, `keyword`, `media_source`, `media_id`, `music_type`, `total_tracks`, `season`, `poster`, `backdrop`, `vote`, `description`, `filter`, `include`, `exclude`, `quality`, `resolution`, `effect`, `audio_quality`, `audio_format`, `min_bitrate`, `min_bit_depth`, `min_sample_rate`, `total_episode`, `start_episode`, `lack_episode`, `note`, `state`, `last_update`, `date`, `username`, `sites`, `downloader`, `best_version`, `best_version_full`, `current_priority`, `current_audio_format`, `current_bitrate`, `current_bit_depth`, `current_sample_rate`, `episode_priority`, `save_path`, `search_imdbid`, `manual_total_episode`, `custom_words`, `media_category`, `filter_groups`, `episode_group`

### `subscribehistory`
- Purpose: Stores snapshots of completed or archived subscriptions and their final filter state.
- Useful queries: Auditing historical subscriptions, media identity, completion criteria, and filter configuration.
- Write boundary: Generated by subscription completion and archival; restore or delete through its business API.
- Columns: `id`, `name`, `year`, `type`, `keyword`, `media_source`, `media_id`, `music_type`, `total_tracks`, `season`, `poster`, `backdrop`, `vote`, `description`, `filter`, `include`, `exclude`, `quality`, `resolution`, `effect`, `audio_quality`, `audio_format`, `min_bitrate`, `min_bit_depth`, `min_sample_rate`, `total_episode`, `start_episode`, `date`, `username`, `sites`, `best_version`, `best_version_full`, `current_priority`, `current_audio_format`, `current_bitrate`, `current_bit_depth`, `current_sample_rate`, `episode_priority`, `save_path`, `search_imdbid`, `custom_words`, `media_category`, `filter_groups`, `episode_group`

### `subscriptionsearchbatch`
- Purpose: Stores durable subscription search batches, source, aggregate state, counts, and cancellation requests.
- Useful queries: Inspecting user-visible search progress, recovery state, cancellation, and terminal outcomes.
- Write boundary: Owned by subscription search orchestration; create and cancel batches through the subscription API.
- Columns: `id`, `batch_id`, `source`, `state`, `priority`, `total_count`, `finished_count`, `failed_count`, `cancelled_count`, `skipped_count`, `cancel_requested`, `created_at`, `updated_at`, `started_at`, `finished_at`, `last_error`

### `subscriptionsearchtask`
- Purpose: Stores one durable subscription search task per batch and subscription with leases and execution phases.
- Useful queries: Diagnosing queued, running, failed, cancelled, or recovered work and its current site.
- Write boundary: Advanced only by the search queue lease state machine; never rewrite leases or terminal states manually.
- Columns: `id`, `task_id`, `batch_id`, `subscription_id`, `active_key`, `source`, `priority`, `position`, `state`, `phase`, `current_site_id`, `attempt_count`, `cancel_requested`, `lease_owner`, `lease_token`, `lease_expires_at`, `available_at`, `created_at`, `updated_at`, `started_at`, `finished_at`, `last_error`

### `subscriptionsitebudget`
- Purpose: Stores per-site subscription search concurrency, cooldown, health, and fairness state.
- Useful queries: Diagnosing site pressure, cooldown deferrals, recent failures, and active search ownership.
- Write boundary: Owned by the subscription site-budget coordinator; do not clear cooldowns or counters by direct SQL.
- Columns: `id`, `site_id`, `lease_owner`, `lease_token`, `lease_expires_at`, `next_allowed_at`, `consecutive_failures`, `success_streak`, `last_outcome`, `last_error`, `updated_at`

### `systemconfig`
- Purpose: Stores JSON business configuration values keyed by SystemConfigKey.
- Useful queries: Verifying the physical value only when the managed settings API behaves unexpectedly.
- Write boundary: Use config.system.get/update first; direct writes bypass validation, events, and plugin admission.
- Columns: `id`, `key`, `value`

### `transferexecutionstep`
- Purpose: Stores intent, attempt identity, state, and result evidence for each durable transfer operation.
- Useful queries: Diagnosing stuck, failed, or repeated steps by task_id or operation_id.
- Write boundary: Owned by the transfer execution state machine and lease CAS; never force state transitions manually.
- Columns: `id`, `task_id`, `operation_id`, `checkpoint_fingerprint`, `ordinal`, `phase`, `kind`, `state`, `attempt_token`, `attempt_count`, `intent_version`, `intent_payload`, `result_version`, `result_payload`, `last_error`, `prepared_at`, `started_at`, `completed_at`, `updated_at`

### `transferhistory`
- Purpose: Stores transfer source, destination, mode, media identity, download linkage, and outcome.
- Useful queries: Reviewing success/failure history, destination paths, media classification, and download linkage.
- Write boundary: Written by transfer settlement; delete or retry through transfer-history business APIs.
- Columns: `id`, `transfer_task_id`, `transfer_settlement_revision`, `src`, `src_storage`, `src_fileitem`, `dest`, `dest_storage`, `dest_fileitem`, `mode`, `type`, `category`, `title`, `year`, `media_source`, `media_id`, `music_type`, `total_tracks`, `audio_format`, `audio_lossless`, `bit_depth`, `sample_rate`, `bitrate`, `seasons`, `episodes`, `image`, `downloader`, `download_hash`, `status`, `errmsg`, `date`, `files`, `episode_group`

### `transferpending`
- Purpose: Durably stores pending transfer input, plans, checkpoints, leases, retries, and manual review state.
- Useful queries: Diagnosing restart recovery, expired leases, retry_wait, terminal failures, or manual review.
- Write boundary: Core durable state machine advanced only by planning, execution, retry, and review services.
- Columns: `id`, `task_id`, `storage`, `src_path`, `created_at`, `state`, `updated_at`, `last_error`, `input_version`, `planning_input`, `input_fingerprint`, `checkpoint_version`, `checkpoint_payload`, `planned_at`, `lease_owner`, `lease_token`, `lease_expires_at`, `heartbeat_at`, `attempt_count`, `execution_state`, `execution_version`, `execution_payload`, `execution_fingerprint`, `retry_generation`, `retry_count`, `retry_due_at`, `retry_requested_by`, `retry_reason`, `settlement_revision`, `terminal_history_id`, `manual_review_revision`, `reviewed_at`, `reviewed_by`, `review_reason`, `review_decision`

### `transfersettlementreceipt`
- Purpose: Stores immutable terminal settlement receipts with contiguous revisions per transfer task.
- Useful queries: Verifying that history, pending deletion, and execution fingerprints were settled reliably.
- Write boundary: Idempotency and audit evidence; append revisions only and never overwrite or delete old receipts.
- Columns: `id`, `task_id`, `history_id`, `settlement_revision`, `outcome`, `execution_fingerprint`, `lease_token`, `history_status`, `src`, `src_storage`, `pending_deleted`, `error`, `created_at`, `updated_at`

### `user`
- Purpose: Stores user accounts, password hashes, administrator state, OTP, permissions, and preferences.
- Useful queries: Authorized diagnostics of account state, permissions, or authentication configuration.
- Write boundary: Security-sensitive; manage through user, permission, password, and two-factor APIs.
- Columns: `id`, `name`, `email`, `hashed_password`, `is_active`, `is_superuser`, `avatar`, `is_otp`, `otp_secret`, `permissions`, `settings`

### `userconfig`
- Purpose: Stores per-user JSON configuration isolated by username and key.
- Useful queries: Inspecting UI preferences, message clear cursors, or other personalized state.
- Write boundary: Modify through the owning user or messaging API to preserve key semantics.
- Columns: `id`, `username`, `key`, `value`

### `workflow`
- Purpose: Stores workflow definitions, triggers, action graphs, execution context, and runtime state.
- Useful queries: Inspecting scheduled/event workflows, pause state, current action, run count, and failures.
- Write boundary: Create, modify, run, pause, or reset through the workflow API.
- Columns: `id`, `name`, `description`, `timer`, `trigger_type`, `event_type`, `event_conditions`, `state`, `current_action`, `result`, `run_count`, `actions`, `flows`, `context`, `execution_config`, `execution_state`, `add_time`, `last_time`

## Database Action Contract

- `tables`: `arguments={}` lists current database tables.
- `schema`: `arguments={"table_name":"downloadhistory"}`; table_name must come from `tables`.
- `query`: `arguments={"sql":"SELECT ...","limit":100,"write":false}`; provide exactly one of sql and file. SELECT/WITH/EXPLAIN are allowed by default.
- `write`: `arguments={"sql":"UPDATE ... WHERE ..."}`; provide exactly one of sql and file and only one statement.
- `file` is a local SQL path readable by the MoviePilot process. MCP clients normally send `sql` directly.

Use the live `schema` result instead of guessing columns from older documentation. Treat `media_source` and `media_id` as one atomic identity pair.

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
