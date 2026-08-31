---
name: organize-files
version: 5
description: >-
  Use this skill when the user asks MoviePilot to identify and organize a local
  or downloaded video/music file, season folder, recording, album directory, or
  mixed folder that automatic transfer did not handle. If failed transfer
  history IDs are supplied, use transfer-failed-retry instead.
allowed-tools: moviepilot_api execute_command ask_user_choice send_message
allowed-api-operations: storage.settings storage.list transfer.history transfer.history.delete media.recognize media.search media.detail library.exists transfer.file media.scrape
---

# Organize Files

Use `moviepilot_api` for every MoviePilot business operation. Retired file,
recognition, transfer, and history tools are not available.

## Workflow

1. Establish scope. If the user provides a path, use it. If they identify a
   downloader task, use `downloader-operation` and its fixed
   `scripts/mp-downloader.py` helper to discover the instance and call
   `tasks.list`. If they only name a configured root, call `storage.settings`.
   Use `storage.list` to inspect the selected directory. Do not process a broad
   shared root without an explicit, bounded scope.
2. Classify files into movie, TV, one music recording, one complete album,
   subtitle/sidecar, or unrelated content. Do not group unrelated media merely
   because they share a directory.
3. Call `media.recognize` with the representative title or path. If uncertain,
   call `media.search`; if several exact candidates remain, use
   `ask_user_choice`. Never invent or translate an ID.
4. Preserve the exact `media_source` + `media_id`. For TV, verify season detail
   with `media.detail` when numbering is ambiguous. For music, a recording is one
   track, an album is one multi-track directory, and an artist is browse-only.
5. When duplicate risk matters, call `library.exists`. If an existing transfer
   record affects reorganization, inspect `transfer.history`.
6. Before a state-changing transfer, summarize the source, target identity,
   media type, season/music entity, storage, and mode. Continue only when the
   user's request already authorizes that exact action or after confirmation.
7. Call `transfer.file` once per verified unit. For an album, transfer the album
   directory once only after its supported audio-file count is consistent with
   the selected album detail.
8. If requested, call `media.scrape` after a successful transfer. Report actual
   tag, cover, and lyrics counts; never assume all lyrics were found.

## Structured Calls

- Directory listing: `storage.list` with storage/path/paging/sort in `body`.
- Recognition: `media.recognize` with title/path in `query`.
- Search: `media.search` with title/type/source constraints in `query`.
- Detail: `media.detail` with `path_params.media_id` and identity/type in `query`.
- Library check: `library.exists` with the exact identity in `query`.
- Transfer: `transfer.file` with the manual-transfer request in `body`.
- Scrape: `media.scrape` with `path_params.storage`, file item in `body`, and
  exact identity/type fields in `query`.

Stop and report instead of transferring when the source is missing, directory
configuration is absent, identity remains ambiguous, an album appears mixed or
incomplete, or the requested target would overwrite unrelated media.
