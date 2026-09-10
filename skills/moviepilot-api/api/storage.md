# Storage APIs

Storage provider settings, browsing, directory management, rename, and delete operations.

## Operations

### `storage.delete`
`POST /api/v1/storage/delete`; policy effect: `destructive_write`.
Purpose: Delete one exact file or directory from a configured storage provider.
- `path_params`: none
- `query`: none
- `body`: `basename` (string|null): Base filename without its parent path.; `children` (array<FileItem-Input>|null): Child storage items nested below this item.; `drive_id` (string|null): Provider-native storage drive identifier.; `extension` (string|null): Filename extension, including or excluding the leading dot as returned by storage.; `fileid` (string|null): Provider-native storage item identifier.; `modify_time` (number|null): Storage item modification timestamp.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `parent_fileid` (string|null): Provider-native identifier of the parent storage directory.; `path` (string|null; default `/`): Storage or history path represented by this record.; `pickcode` (string|null): 115 storage pickcode associated with the item.; `size` (integer|null): File or torrent size in bytes.; `storage` (string|null; default `local`): Configured storage name or storage type used by the operation.; `thumbnail` (string|null): Thumbnail URL returned by the storage provider.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `url` (string|null): Site, storage, or torrent URL represented by this field.

### `storage.list`
`POST /api/v1/storage/agent/list`; policy effect: `safe_read`.
Purpose: List files or directories from one configured storage location.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `sort` (string|null; default `updated_at`): Storage-list sort field or ordering expression.
- `body`: `basename` (string|null): Base filename without its parent path.; `children` (array<FileItem-Input>|null): Child storage items nested below this item.; `drive_id` (string|null): Provider-native storage drive identifier.; `extension` (string|null): Filename extension, including or excluding the leading dot as returned by storage.; `fileid` (string|null): Provider-native storage item identifier.; `modify_time` (number|null): Storage item modification timestamp.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `parent_fileid` (string|null): Provider-native identifier of the parent storage directory.; `path` (string|null; default `/`): Storage or history path represented by this record.; `pickcode` (string|null): 115 storage pickcode associated with the item.; `size` (integer|null): File or torrent size in bytes.; `storage` (string|null; default `local`): Configured storage name or storage type used by the operation.; `thumbnail` (string|null): Thumbnail URL returned by the storage provider.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `url` (string|null): Site, storage, or torrent URL represented by this field.

### `storage.manage`
`POST /api/v1/storage/manage`; policy effect: `external_side_effect`.
Purpose: Run one provider-defined management action against an exact configured storage target.
- `path_params`: none
- `query`: none
- `body`: `action*` (string): Exact provider or workflow action identifier required by the selected operation.; `params` (object): Provider-defined JSON parameters for the selected authentication or storage action.; `target*` (string): Classification rule output containing a category ID and optional labels.

### `storage.mkdir`
`POST /api/v1/storage/mkdir`; policy effect: `reversible_write`.
Purpose: Create a named child directory below one exact storage directory item.
- `path_params`: none
- `query`: `name*` (string): Human-readable name of the site, storage item, subscription, or rule group.
- `body`: `basename` (string|null): Base filename without its parent path.; `children` (array<FileItem-Input>|null): Child storage items nested below this item.; `drive_id` (string|null): Provider-native storage drive identifier.; `extension` (string|null): Filename extension, including or excluding the leading dot as returned by storage.; `fileid` (string|null): Provider-native storage item identifier.; `modify_time` (number|null): Storage item modification timestamp.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `parent_fileid` (string|null): Provider-native identifier of the parent storage directory.; `path` (string|null; default `/`): Storage or history path represented by this record.; `pickcode` (string|null): 115 storage pickcode associated with the item.; `size` (integer|null): File or torrent size in bytes.; `storage` (string|null; default `local`): Configured storage name or storage type used by the operation.; `thumbnail` (string|null): Thumbnail URL returned by the storage provider.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `url` (string|null): Site, storage, or torrent URL represented by this field.

### `storage.rename`
`POST /api/v1/storage/rename`; policy effect: `reversible_write`.
Purpose: Rename one exact storage item, optionally applying media-aware recursive renaming.
- `path_params`: none
- `query`: `new_name*` (string): Replacement name for the existing filter-rule group.; `recursive` (boolean|null; default `False`): Apply media-aware renaming recursively to child files when true.
- `body`: `basename` (string|null): Base filename without its parent path.; `children` (array<FileItem-Input>|null): Child storage items nested below this item.; `drive_id` (string|null): Provider-native storage drive identifier.; `extension` (string|null): Filename extension, including or excluding the leading dot as returned by storage.; `fileid` (string|null): Provider-native storage item identifier.; `modify_time` (number|null): Storage item modification timestamp.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `parent_fileid` (string|null): Provider-native identifier of the parent storage directory.; `path` (string|null; default `/`): Storage or history path represented by this record.; `pickcode` (string|null): 115 storage pickcode associated with the item.; `size` (integer|null): File or torrent size in bytes.; `storage` (string|null; default `local`): Configured storage name or storage type used by the operation.; `thumbnail` (string|null): Thumbnail URL returned by the storage provider.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `url` (string|null): Site, storage, or torrent URL represented by this field.

### `storage.settings`
`GET /api/v1/storage/directories`; policy effect: `safe_read`.
Purpose: Read configured directory or storage settings.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `directory_type` (string; default `all`): Directory configuration subtype to return.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `storage_type` (string; default `all`): Configured storage provider type to return.
- `body`: none
