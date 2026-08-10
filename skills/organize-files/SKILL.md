---
name: organize-files
version: 2
description: >-
  Use this skill when the user asks the MoviePilot agent to identify and organize downloaded/local video or music files that automatic transfer cannot handle. Typical triggers include manually organizing a file or folder, a TV season pack, one music recording, or a complete album directory. If the user gives failed transfer history IDs, prefer transfer-failed-retry instead.
allowed-tools: list_directory query_directory_settings query_download_tasks query_transfer_history delete_transfer_history recognize_media search_media query_media_detail query_library_exists transfer_file scrape_metadata ask_user_choice send_message
---

# Organize Files (智能整理文件)

Use this skill to help the user identify media files that MoviePilot could not organize automatically, then call the normal transfer pipeline through `transfer_file`. Do not rename, move, or copy files manually; let MoviePilot's directory, transfer mode, rename template, overwrite, scrape, and notification settings handle the actual organization.

## MoviePilot Transfer Flow

MoviePilot's normal flow is:

1. `DownloadChain.download_single` adds a downloader task, records `DownloadHistory` and `DownloadFiles`, runs downloader-specific `download_added`, then sends `DownloadAdded`.
2. `TransferChain.process` scans completed downloader tasks in monitored download directories. If a `DownloadHistory` exists for the hash, it reuses the recorded media IDs; otherwise it falls back to path recognition.
3. Agent/manual organization calls `transfer_file`, which enters `TransferFileTool` -> `TransferChain.manual_transfer` -> `TransferChain.do_transfer`.
4. `do_transfer` recursively collects eligible video/subtitle/audio files, ignores recycle/hidden paths and configured exclude words, and reuses download history when possible. Video uses `MetaInfoPath`; music uses audio tags plus `MetaMusic`/`MusicInfo` and keeps the selected recording or album identity.
5. `TransferChain.__handle_transfer` chooses the target directory through `DirectoryHelper`, delegates file operations to the file manager module, and lets `TransHandler` build the final target path and name.
6. The callback writes `TransferHistory` success/failure records, emits transfer events, sends notifications, and may trigger `transfer-failed-retry` for failed history records.

Important implication: an existing `TransferHistory` for the same source path can make a later transfer skip. Delete only stale or failed history records, and only after the user has confirmed the record is safe to remove.

## Workflow

### 1. Classify The Request

- If the user provides one or more failed transfer history IDs, stop and use `transfer-failed-retry`.
- If the user provides a path, start from that path.
- If the user describes a download task, use `query_download_tasks` to find its save path or hash, then continue with the path.
- If the user only says "整理一下下载目录", use `query_directory_settings(directory_type="download")` first, then ask which directory or subdirectory to process if more than one candidate exists.

### 2. Inspect Candidate Files

Use `list_directory` for any directory the user provides. Prefer `sort_by="time"` for "recent" or "刚下载的" requests.

For directories with more than 20 items, ask the user to narrow the folder or choose the relevant child directory before running transfers. Avoid organizing a broad shared download root unless the user explicitly confirms the scope.

Treat these as transfer candidates:

- main media files and Blu-ray folders;
- matching subtitle and external audio files in the same media folder;
- episode packs where files share the same title/season pattern.
- individual supported audio files and album folders containing multiple tracks.

Skip obvious samples, trailers, screenshots, hidden folders, recycle folders, and files that are not media/subtitle/audio.

### 3. Identify The Media

For the best sample file, call:

```text
recognize_media(path="<source file path>")
```

If recognition fails or looks wrong:

1. Extract likely title, year, media type, season/episode range, or music artist/track/album from filenames and audio tags.
2. For video, call `search_media(title="...", year="...", media_type="movie|tv")`. For music, call `search_media(title="<artist> - <title>", media_type="music", music_type="recording|album")`.
3. If several results are plausible, use `ask_user_choice` when available, or ask the user directly to choose the correct title/TMDB ID.
4. For TV season confusion, use `query_media_detail(tmdb_id=<id>, media_type="tv")` before deciding the season number. For an album, use `query_media_detail(media_type="music", music_type="album", media_source="musicbrainz", media_id="<album_id>")` and verify `total_tracks` before treating the directory as complete.

Never invent an ID. Preserve the exact source-native entity returned by search: a recording is one track, an album is a multi-track collection, and an artist is browse-only and cannot be organized.

### 4. Check Existing State

Before writing:

- Use `query_library_exists` when a precise video or music identity is known and duplicate risk matters. For albums, an exists result is only true after complete track coverage is confirmed.
- Use `query_transfer_history(title="<title or path keyword>", status="all")` if the file may already have a success or failure record.
- If `transfer_file` later returns "已整理过", query transfer history, identify the matching source path, and ask before deleting the stale record.

Only call `delete_transfer_history(history_id=<id>)` for the exact stale/failed record that blocks the requested source path. Do not delete unrelated successful history.

### 5. Transfer Through MoviePilot

Use `transfer_file` with explicit identity whenever possible:

```text
transfer_file(
  file_path="<source path>",
  storage="local",
  media_type="movie|tv",
  tmdbid=<tmdb_id>,
  season=<season_number_if_tv>
)
```

For one recording:

```text
transfer_file(file_path="<audio file>", media_type="music", music_type="recording", media_source="musicbrainz", media_id="<recording_id>")
```

For a complete album, transfer the album directory once:

```text
transfer_file(file_path="<album directory>/", media_type="music", music_type="album", media_source="musicbrainz", media_id="<album_id>")
```

Rules:

- For directories, pass a trailing slash in `file_path` so the tool treats it as a directory.
- Prefer leaving `target_path`, `target_storage`, and `transfer_type` empty so configured directory rules apply.
- Set `target_path` or `transfer_type` only when the user explicitly asks or the default directory configuration cannot handle the file.
- For a single movie or a single TV season folder, transfer the folder once with the shared identity.
- For mixed folders, split by media and transfer each file/subfolder separately.
- For episode packs, identify the media once, then reuse `tmdbid`, `media_type="tv"`, and the confirmed `season` for each item.
- For one recording, transfer only that audio file with the recording ID.
- For one album, verify the directory belongs to the selected album, then transfer the directory once with the album ID. Do not submit every track as an unrelated recording.
- Never transfer an artist search result. Select a recording or album first.
- When the user asks to refresh music tags, cover, or lyrics after transfer, call `scrape_metadata(media_type="music", ...)`; album scraping may use the album ID and reports actual lyrics counts.

### 6. Report Clearly

After each transfer batch, report:

- source path(s) processed;
- recognized media title, type, TMDB/Douban ID, season/episode range when relevant;
- success/failure count;
- any failed message exactly enough for the user to act, such as missing media library directory, unsupported storage, existing history, or no media recognized.

If the result creates failed history records, tell the user they can retry with the history ID or let the agent continue with `transfer-failed-retry`.

## Common Cases

### User Gives A Single File

1. `recognize_media(path=...)`
2. If needed, `search_media(...)` and confirm the result.
3. `transfer_file(file_path=..., media_type=..., tmdbid=..., season=...)`

### User Gives A Season Folder

1. `list_directory(path=...)`
2. Pick a representative episode and run `recognize_media(path=...)`.
3. Confirm `tmdbid`, `media_type="tv"`, and season.
4. `transfer_file(file_path="<folder>/", media_type="tv", tmdbid=<id>, season=<season>)`

### User Gives One Music Track

1. `recognize_media(path=..., media_type="music")`
2. Confirm the artist and recording title; use `search_media(..., music_type="recording")` when ambiguous.
3. Check the exact recording with `query_library_exists` when duplicate risk matters.
4. Transfer the audio file once with the recording `media_source` + `media_id`.

### User Gives An Album Folder

1. `list_directory(path=...)` and confirm the files form one album rather than a mixed folder.
2. Recognize a representative track, then search/select the album entity and query album detail.
3. Compare the folder's supported audio-file count with album `total_tracks`; ask before proceeding when the folder appears incomplete or mixed.
4. Check album library existence, then transfer the directory once with `media_type="music"`, `music_type="album"`, and the album identity.
5. If requested, scrape the album directory for configured tags, cover, and lyrics; do not claim every lyric was found unless the tool reports it.

### User Gives A Messy Mixed Folder

1. `list_directory(path=...)`
2. Group candidates by likely title/year/season.
3. Confirm groups before writing if there is more than one media.
4. Transfer each group separately; do not run one directory transfer over unrelated media.

### Transfer Says The File Was Already Organized

1. `query_transfer_history(title="<title or source path keyword>", status="all")`
2. Find the exact record with matching `src`.
3. Ask the user to confirm deletion if the record is stale or failed.
4. `delete_transfer_history(history_id=<id>)`
5. Retry `transfer_file(...)`.

## Guardrails

- Do not use shell commands, raw database edits, or manual filesystem moves for organization.
- Do not delete transfer history without an exact matching source path and user confirmation.
- Do not use broad download roots as transfer targets unless the user explicitly confirms the scope.
- Do not process unrelated media in one directory transfer.
- Do not confuse a same-name recording, album, and artist; preserve `music_type` and source-native IDs.
- Do not report a partial album as complete or present in the library.
- Do not override target directories or transfer modes unless necessary.
- Prefer asking one focused question over guessing media identity, season mapping, or destructive cleanup.
