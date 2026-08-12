---
name: transfer-failed-retry
version: 4
description: Use this skill when you need to retry failed video or music transfers/organizations. Given failed transfer history IDs, query the exact records, group by trustworthy movie/series/recording/album identity, delete only the old records being retried, then re-identify and re-organize through MoviePilot. This skill is automatically triggered when transfer failures occur and AI retry is enabled.
allowed-tools: query_transfer_history delete_transfer_history recognize_media transfer_file search_media
---

# Transfer Failed Retry (整理失败重试)

This skill handles retrying failed file transfers/organizations. When file transfers fail, you can use this skill to analyze the failures, remove stale history records, and attempt to re-identify and re-organize the files. It supports both single-file and batch retry scenarios.

## Prerequisites

You need the following tools:
- `query_transfer_history` - Query transfer history records
- `delete_transfer_history` - Delete a transfer history record
- `recognize_media` - Recognize media info from file path or title
- `transfer_file` - Transfer/organize files to the media library
- `search_media` - Search video metadata or MusicBrainz recording/album/artist candidates

## Workflow

### Step 1: Query the Failed Transfer History

Use `query_transfer_history` to get details about the failed record(s). Filter by status `failed` to find the specific records.

If you are given a specific history record ID (or multiple IDs), query with those IDs to understand the failure context:

```
query_transfer_history(status="failed")
```

From each record, extract the following key information:
- **id**: The history record ID
- **src**: Source file path
- **title**: The recognized title (may be incorrect)
- **errmsg**: The error message explaining why the transfer failed
- **type**: Media type (movie/tv/music)
- **media_source/media_id**: Exact source-native identity; preserve the pair together for every retry
- **seasons/episodes**: Season/episode info (if TV show)
- **downloader**: Which downloader was used
- **download_hash**: The torrent hash

### Step 2: Analyze the Failure Reason

Common failure reasons and how to handle them:

| Error Message | Cause | Solution |
|---------------|-------|----------|
| 未识别到媒体信息 | File name or audio tags could not be matched | Use `search_media` to find the exact `media_source` + `media_id`, then transfer with that pair |
| 源目录不存在 | Source file was moved or deleted | Cannot retry - skip this record |
| 目标路径不存在 | Target directory issue | Retry transfer - the directory config may have been fixed |
| 文件已存在 | Target file already exists | May need to use `force` mode or skip |
| 未找到有效的集数信息 | Episode number not recognized | Use `recognize_media` with the file path to get better metadata, or specify season/episode in `transfer_file` |
| 未获取到转移目录设置 | No transfer directory configured for this media type | Cannot auto-fix - notify user about directory configuration |

### Step 3: Delete the Failed History Record(s)

Before an agent-driven retry, delete the exact failed history record(s) so the cleanup is explicit and auditable. The interactive manual-transfer flow now clears matching failed records automatically, but agent retries retain this confirmation step.

```
delete_transfer_history(history_id=<record_id>)
```

### Step 4: Re-identify and Re-organize

Based on the failure analysis in Step 2:

#### Case A: Unrecognized Media (未识别到媒体信息)

1. Try recognizing the media from file path:
   ```
   recognize_media(path="<source_file_path>")
   ```

2. If recognition fails, search the appropriate metadata source with keywords extracted from the filename or audio tags:
   ```
   search_media(title="<extracted_title>", media_type="movie" or "tv")
   # or for music
   search_media(title="<artist> - <track_or_album>", media_type="music", music_type="recording" or "album")
   ```

3. Once you have the exact identity, re-transfer with explicit identification:
   ```
   transfer_file(file_path="<source_path>", media_source="<source>", media_id="<native_id>", media_type="movie" or "tv")
   # or for music
   transfer_file(file_path="<source_path>", media_type="music", music_type="recording" or "album", media_source="musicbrainz", media_id="<recording_or_album_id>")
   ```

#### Case B: Transfer Error (file operation failed)

Simply retry the transfer:
```
transfer_file(file_path="<source_path>")
```

#### Case C: Episode Recognition Issue

For TV shows where episode info couldn't be determined:
1. Use `recognize_media` to get better metadata
2. Re-transfer with explicit season info:
   ```
   transfer_file(file_path="<source_path>", media_source="<source>", media_id="<native_id>", media_type="tv", season=<season_number>)
   ```

#### Case D: Music Recording Or Album

1. A recording is one track. Retry the individual audio file with its recording ID.
2. An album is a collection like a TV season pack. If several failed tracks share one album directory and album ID, verify the group and retry the directory once with the album ID.
3. Never use an artist ID as a transfer target. Search/select a recording or album instead.
4. Do not infer that a directory is complete merely because it has multiple files. Preserve the album identity and let the transfer/download pipeline enforce expected-track semantics where available.

### Step 5: Report Result

After the retry attempt, report the result:
- If successful: Confirm the file(s) have been organized correctly
- If failed again: Report the new error and suggest manual intervention
- For batch operations: Report a summary (e.g., "成功 8/10，失败 2/10")

## Batch Processing (批量处理)

When multiple files fail simultaneously (for example, TV episodes or tracks from one album), the system may trigger one batch retry. Treat the batch as candidates for grouping, not proof that every record has the same identity.

### Key Optimization Rules for Batch Processing:

1. **Group first, identify once per verified group**: Group by source directory and exact media identity. Reuse video IDs within one movie/series group and reuse an album ID for tracks from one album. Do not apply one recording ID to multiple different tracks.

2. **Choose the correct retry unit**: For movies, recordings, and TV episode files, delete and retry each exact failed record/file as needed. For a verified album directory, delete the selected failed records and submit the album directory once rather than repeatedly transferring every track.
   - Delete each failed history record individually
   - Transfer each file individually (they have different source paths)

3. **Stop early if root cause is unfixable**: If the first file fails due to an unfixable issue (e.g., missing directory configuration), skip all remaining files with the same error rather than retrying each one.

4. **Process in order**: Handle files sequentially to avoid race conditions.

### Batch Example Flow:

```
# Given failed records: IDs = [42, 43, 44, 45] (4 episodes of the same show)
# All have errmsg="未识别到媒体信息"

# 1. Query all failed records
query_transfer_history(status="failed")

# 2. Identify media ONCE using the first file
recognize_media(path="/downloads/Show.Name.S01E01.1080p.mkv")
# Found: media_source="themoviedb", media_id="789", media_type="tv"

# 3. For each record: delete history, then re-transfer
delete_transfer_history(history_id=42)
transfer_file(file_path="/downloads/Show.Name.S01E01.1080p.mkv", media_source="themoviedb", media_id="789", media_type="tv")

delete_transfer_history(history_id=43)
transfer_file(file_path="/downloads/Show.Name.S01E02.1080p.mkv", media_source="themoviedb", media_id="789", media_type="tv")

delete_transfer_history(history_id=44)
transfer_file(file_path="/downloads/Show.Name.S01E03.1080p.mkv", media_source="themoviedb", media_id="789", media_type="tv")

delete_transfer_history(history_id=45)
transfer_file(file_path="/downloads/Show.Name.S01E04.1080p.mkv", media_source="themoviedb", media_id="789", media_type="tv")

# 4. Report summary: "重试完成：4/4 成功"
```

## Important Notes

- **Always delete the old history record first** in this agent workflow so the destructive cleanup remains explicit, even though the interactive manual-transfer flow can clear failed history automatically.
- **Do not retry** if the source file no longer exists (源目录不存在).
- **Do not retry** if the error is about missing directory configuration - this requires user intervention.
- **For unrecognized media**, always try `recognize_media` with the file path first before falling back to `search_media`.
- **Be cautious with TV shows** - ensure the correct season and episode information is used.
- **For batch processing**, reuse media identification only inside a verified group. Same source location alone does not prove shared identity.
- **For music**, keep recording, album, and artist semantics distinct. Artists are browse-only; albums are multi-track retry units.
- When this skill is triggered automatically by the system, it provides the `history_id`(s) directly. Start from Step 1 with those specific IDs.

## Example: Single File Retry Flow

```
# 1. Query the failed record
query_transfer_history(status="failed", page=1)
# Found: id=42, src="/downloads/Movie.Name.2024.1080p.mkv", errmsg="未识别到媒体信息"

# 2. Try to recognize the media from path
recognize_media(path="/downloads/Movie.Name.2024.1080p.mkv")
# Recognition failed

# 3. Search TMDB
search_media(title="Movie Name", year="2024", media_type="movie")
# Found: media_source="themoviedb", media_id="123456"

# 4. Delete old history record
delete_transfer_history(history_id=42)

# 5. Re-transfer with correct identification
transfer_file(file_path="/downloads/Movie.Name.2024.1080p.mkv", media_source="themoviedb", media_id="123456", media_type="movie")
# Success!
```
