---
name: transfer-failed-retry
version: 6
description: >-
  Analyze failed MoviePilot transfers, verify media identity and season/episode,
  and safely replan durable recognition rejections while preserving task identity,
  execution evidence, and retry budgets.
allowed-tools: moviepilot_api
allowed-api-operations: >-
  transfer.history transfer.history.replan transfer.history.delete media.recognize
  media.search media.detail media.seasons storage.list transfer.file
---

# Transfer Failed Retry

Use structured `moviepilot_api` operations only; discover their current schemas.

## Required Flow

1. Inspect every exact requested history ID. Record source/storage, destination,
   mode, identity, season/episode, status, error, and durable task context.
   Treat unknown task ownership as durable, and never act on another record.
2. Verify source availability with `storage.list`. Stop for missing files,
   storage/permission/configuration faults, manual review, uncertain effects,
   or exhausted retry budgets. Do not delete targets or change global rules.
3. Analyze the filename and error. Verify identity with `media.recognize` or
   `media.search`, and check TV season/episode with `media.detail` or
   `media.seasons`. Do not reuse a wrong failed identification unchanged.
   `Season 2 - 03` requires verifying S02E03 rather than assuming S01.
   Group only proven shared identities; verify each file's season/episode.
4. Preview with `transfer.file` and `preview=true` using the original source,
   destination, mode, and verified identity and season/episode. Inspect the
   preview before submitting any correction.
5. For durable records, use `transfer.history.replan` with the exact history ID,
   original `task_id` and `settlement_revision` from the supplied context, verified `media_source`, `media_id`, `media_type` (`movie`/`tv`), and optional
   `season`/`episode`. It accepts only failed planning rejections with no file
   effects. It preserves the task, original plan/step evidence, settlement
   receipts, source deduplication, and consumed retry budget.
6. Stop on a replan rejection and report the blocker. Never use
   `transfer.history.delete`, ordinary redo, a new non-preview `transfer.file`,
   target cleanup, or evidence deletion to bypass a durable task's checks.
   Music and tasks with file-operation evidence require manual recovery.
7. Only records explicitly marked `legacy` may call `transfer.history.delete`
   without deleting source/target files, then continue with `transfer.file`
   after the response confirms history deletion. Preserve verified identity,
   original storage/mode/destination, season/episode, and music entity fields.
   A verified complete album sharing one directory may be retried once as an
   album; an artist is never a transfer target.
8. Report verified corrections, accepted/queued replans, confirmed transfer
   successes, and remaining blockers accurately. A queued replan is not a
   completed transfer. Never reset or remove failure/retry evidence.
