---
name: transfer-failed-retry
version: 5
description: >-
  Use this skill for failed MoviePilot video or music transfer history IDs.
  Inspect the exact records, preserve durable retry evidence, group only records
  with a trustworthy shared identity, and re-identify/reorganize only legacy
  records that were actually deleted.
allowed-tools: moviepilot_api
allowed-api-operations: transfer.history transfer.history.delete media.recognize media.search transfer.file
---

# Transfer Failed Retry

Use structured `moviepilot_api` operations only.

## Required Flow

1. Call `transfer.history` with `status=failed` and locate every requested ID.
   Record source path/storage, destination, mode, exact identity, music type,
   season/episode, status, and error. Do not act on a different record.
2. If the source no longer exists or transfer-directory configuration is
   missing, stop for that record and report the blocker.
3. Group records only when identity and source layout prove they belong to the
   same movie, series, recording, or album. Same parent directory alone is not
   enough. Recognize once per verified group.
4. Call `transfer.history.delete` for each exact history ID before retrying.
   This operation may submit a durable record to the persistent retry scheduler
   instead of deleting it.
5. If the result says durable retry was accepted or rejected, that result is
   final for the current task. Do not call `transfer.file`, delete the target, or
   remove history/retry evidence.
6. Only when the response confirms a legacy history was actually deleted may
   you continue. First call `media.recognize` with the source path. If the result
   is absent or unreliable, call `media.search` with narrow filename/tag facts.
7. Call `transfer.file` with the original source/storage/mode and the verified
   source-native identity. Preserve season and music entity fields. For a
   verified complete album sharing one directory, retry the directory once, not
   each track as unrelated media.
8. Report accepted durable retries, successful legacy retransfers, skipped
   missing/configuration cases, and remaining failures separately.

Never transfer an artist entity. Never reuse one identification across records
that do not form a verified group. Never report a queued durable retry as a
completed file transfer.
