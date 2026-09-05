"""从业务 OpenAPI 构建 moviepilot_api 的外部 MCP 输入合同。"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Mapping, Sequence

OPERATION_DESCRIPTIONS = {
    "config.identifiers.get": "Read the complete custom media-recognition identifier list.",
    "config.identifiers.update": "Replace the complete custom media-recognition identifier list.",
    "config.system.get": "Discover registered system settings or read one exact setting.",
    "config.system.update": "Update one exact registered system setting.",
    "download.add": "Submit one torrent to MoviePilot's normal download workflow.",
    "download.clients": "List enabled downloader instance names and provider types without credentials.",
    "download.history.list": "Page MoviePilot download-history records in reverse chronological order.",
    "download.history.delete": "Delete one MoviePilot download-history record.",
    "download.paths": "List configured downloader save-path URIs that may be passed to download.add.",
    "download.tasks.active": "List currently downloading MoviePilot tasks with their canonical media context.",
    "filter.builtin": "List built-in torrent filter rules.",
    "filter.custom": "List user-defined torrent filter rules.",
    "filter.custom.add": "Create one user-defined torrent filter rule.",
    "filter.custom.delete": "Delete one user-defined torrent filter rule.",
    "filter.custom.update": "Update one user-defined torrent filter rule.",
    "filter.group.add": "Create one named filter-rule group.",
    "filter.group.delete": "Delete one named filter-rule group.",
    "filter.group.update": "Update or rename one named filter-rule group.",
    "filter.groups": "List named filter-rule groups.",
    "library.exists": "Check configured media servers for one canonical media identity.",
    "library.latest": "List recently added items from one configured media-server instance for the current user.",
    "media.detail": "Read canonical media details from one selected metadata source.",
    "media.episode_schedule": "Read TMDB episode release information for one season.",
    "media.person.credits": "Read one person's credits from the selected metadata source.",
    "media.person.search": "Search people across selected metadata sources.",
    "media.recognize": "Recognize media identity from a title, subtitle, or custom rule context.",
    "media.scrape": "Generate or refresh metadata for one storage item.",
    "media.search": "Search canonical media across selected metadata sources.",
    "music.album.get": "Read one album's details, tracks, releases, and aligned artist names and IDs.",
    "music.album.related": "Browse albums related to one source-native album identity.",
    "music.artist.albums": "Browse one artist's albums, singles, EPs, or another exact release-group type.",
    "music.artist.get": "Read one artist's canonical details from the selected music metadata source.",
    "music.artist.related": "Browse artists related to one source-native artist identity.",
    "music.cache.clear": "Clear the complete administrator-only MusicBrainz recognition cache.",
    "music.cache.delete": "Delete one administrator-only MusicBrainz recognition-cache entry by exact key.",
    "music.cache.get": "Inspect the administrator-only MusicBrainz recognition cache and summary counts.",
    "music.explore": "Browse MusicBrainz charts or fresh releases, or Douban Music tag categories.",
    "music.recognize": "Resolve one recording or album from an exact music source and source-native ID.",
    "plugin.capabilities": "Inspect the runtime capabilities exposed by installed plugins.",
    "plugin.config.get": "Read one loaded plugin's configuration form and its defaults merged with saved values.",
    "plugin.config.update": "Replace one installed plugin's complete configuration and apply it immediately.",
    "plugin.data": "Read a bounded preview of one plugin's persisted data.",
    "plugin.install": "Install or update one plugin from an approved source.",
    "plugin.installed": "List installed plugins and their runtime status.",
    "plugin.market": "List plugins available from configured marketplaces.",
    "plugin.reload": "Reload one installed plugin into the running process.",
    "plugin.source.change": "Switch an installed plugin to one explicitly selected online source revision.",
    "plugin.source.install": "Install an unbound plugin from one explicitly selected online source.",
    "plugin.source.options": "Inspect source candidates and the current immutable source identity before installation or source change.",
    "plugin.uninstall": "Uninstall one plugin and remove it from the installed set.",
    "recommendation.list": "Read personalized media or music recommendations.",
    "scheduler.list": "List registered scheduler jobs and their current state.",
    "scheduler.run": "Run one registered scheduler job immediately.",
    "search.results": "Read the most recent torrent-search context and result set.",
    "search.torrents": "Search torrent sites for one canonical media identity.",
    "site.cookie.update": "Log in to one site and refresh its stored authentication cookie.",
    "site.list": "List configured sites with status/name filters; authentication fields are returned only to a superuser.",
    "site.test": "Test connectivity and authentication for one configured site.",
    "site.update": "Update one configured site's complete settings.",
    "site.userdata": "Read the latest account statistics collected from one site.",
    "slash.list": "List slash commands that the Agent may dispatch.",
    "slash.run": "Execute one complete slash command through MoviePilot messaging.",
    "storage.list": "List files or directories from one configured storage location.",
    "storage.settings": "Read configured directory or storage settings.",
    "subscription.add": "Create one movie, TV, or music subscription.",
    "subscription.delete": "Delete one active subscription.",
    "subscription.history": "List completed or archived subscription records.",
    "subscription.list": "List active subscriptions.",
    "subscription.popular": "List globally popular subscriptions with filters and pagination.",
    "subscription.search": "Run an immediate search for one existing subscription.",
    "subscription.shares": "List shared subscriptions with filters and pagination.",
    "subscription.update": "Update one existing movie, TV, or music subscription.",
    "system.restart": "Restart the running MoviePilot process.",
    "system.update.check": "Check for the latest stable MoviePilot v3 application release and current-platform site resources.",
    "system.update.download": "Start downloading and verifying one selected application or site-resource update in the background.",
    "system.update.install": "Install one selected already downloaded and verified application or site-resource update, then restart MoviePilot.",
    "system.update.status": "Read application and site-resource update checks, downloads, verification, or install state.",
    "system.upgrade.dev": "Update to the current v3 development branch and restart MoviePilot.",
    "system.versions": "List available MoviePilot GitHub releases.",
    "transfer.file": "Run MoviePilot's manual file-transfer and organization workflow.",
    "transfer.history": "List file-transfer history with filters and pagination.",
    "transfer.history.delete": "Delete one transfer-history record and optionally remove files.",
    "workflow.list": "List configured workflows and their execution state.",
    "workflow.run": "Run one configured workflow from the beginning or resume point.",
    "dashboard.cpu": "Read the current host CPU utilization percentage.",
    "dashboard.downloader": "Read aggregate downloader task counts, speeds, and free-space information.",
    "dashboard.media.statistics": "Read aggregate movie, TV, episode, and music library counts.",
    "dashboard.memory": "Read current MoviePilot process and host memory utilization.",
    "dashboard.network": "Read the current host network receive and transmit counters.",
    "dashboard.processes": "List host processes visible to the MoviePilot runtime.",
    "dashboard.storage": "Read local filesystem capacity and free-space information.",
    "dashboard.system": "Read MoviePilot host, runtime, platform, and uptime summary information.",
    "dashboard.transfer.statistics": "Read aggregate file-transfer counts grouped by time period.",
    "database.backups.create": "Create, verify, and atomically publish a managed database backup.",
    "database.backups.delete": "Delete one exact managed database backup artifact.",
    "database.backups.list": "List managed database backup artifacts without exposing host paths.",
    "database.backups.verify": "Verify the integrity of one exact managed database backup artifact.",
    "filter.test": "Test one title and optional subtitle against an exact named filter-rule group.",
    "media.categories": "Read the resolved automatic media-category mapping.",
    "media.category.config.get": "Read the complete automatic media-category strategy configuration.",
    "media.episode_group.seasons": "List seasons defined by one exact TMDB episode-group identity.",
    "media.episode_groups": "List alternate TMDB episode groups available for one TV media identity.",
    "media.recognize_file": "Recognize canonical media identity from one exact filename and optional path context.",
    "media.seasons": "List seasons for one exact media identity or a title-and-year fallback.",
    "media.sources": "List metadata sources currently registered for MoviePilot media operations.",
    "plugin.clone": "Create a configurable clone of one installed plugin.",
    "plugin.history": "Read marketplace update notes and history for one plugin.",
    "plugin.market.sync_wiki": "Refresh the configured plugin marketplace repositories from the MoviePilot Wiki.",
    "plugin.rating": "Read the current aggregate rating for one plugin.",
    "plugin.rating.submit": "Submit or replace the current user's rating for one plugin.",
    "plugin.ratings": "Read aggregate ratings for a requested plugin set.",
    "plugin.releases": "List available release versions for one plugin source.",
    "plugin.reset": "Delete one plugin's saved configuration and data, then restore its default runtime state.",
    "plugin.runtime.status": "Read plugin runtime convergence, loading, and failure state.",
    "plugin.statistics": "Read public installation statistics for plugins.",
    "scheduler.progress": "Read current progress for one exact scheduler job.",
    "search.recommend": "Use the configured recommendation model to rank or recommend torrent search results.",
    "search.title": "Search torrent sites directly from a free-form title and optional media filters.",
    "site.add": "Create one configured site with its complete authentication and search settings.",
    "site.auth.options": "List site-account authentication providers and their required input definitions.",
    "site.authenticate": "Authenticate a supported site account and persist the resulting site authorization state.",
    "site.category": "List torrent categories supported by one configured site.",
    "site.cookiecloud.sync": "Start a CookieCloud synchronization of configured sites.",
    "site.delete": "Delete one configured site by persistent site ID.",
    "site.mapping": "Read the configured site-domain to site-name mapping.",
    "site.priorities.update": "Replace priorities for the supplied configured site IDs.",
    "site.reset": "Delete all configured sites and start a fresh CookieCloud synchronization.",
    "site.resource": "Browse torrent resources from one configured site with category and keyword filters.",
    "site.rss": "List configured sites selected for RSS subscription processing.",
    "site.searchable": "List active configured sites supporting one exact media type.",
    "site.statistic": "Read account and traffic statistics for one exact configured site domain.",
    "site.statistics": "Read the latest account and traffic statistics for all configured sites.",
    "site.supporting": "List indexer definitions supported by the installed MoviePilot resources.",
    "site.userdata.latest": "Read the latest collected account statistics for every configured site.",
    "site.userdata.refresh": "Refresh and return account statistics for one configured site.",
    "storage.delete": "Delete one exact file or directory from a configured storage provider.",
    "storage.manage": "Run one provider-defined management action against an exact configured storage target.",
    "storage.mkdir": "Create a named child directory below one exact storage directory item.",
    "storage.rename": "Rename one exact storage item, optionally applying media-aware recursive renaming.",
    "subscription.delete_by_media": "Delete accessible subscriptions matching one canonical media identity.",
    "subscription.files": "Read local library and transfer-file coverage for one accessible subscription.",
    "subscription.find": "Find one accessible subscription by canonical media identity and optional season.",
    "subscription.follow.add": "Follow one subscription-sharing user by exact share user ID.",
    "subscription.follow.delete": "Stop following one subscription-sharing user by exact share user ID.",
    "subscription.follow.list": "List subscription-sharing user IDs followed by the current user.",
    "subscription.fork": "Create a local subscription from one shared subscription definition.",
    "subscription.get": "Read one accessible subscription by persistent subscription ID.",
    "subscription.history.delete": "Delete one accessible subscription-history record.",
    "subscription.metadata.refresh": "Start a system-wide refresh of subscription TMDB metadata.",
    "subscription.refresh": "Start the configured system-wide subscription refresh job.",
    "subscription.reset": "Reset one accessible subscription so it can be processed again.",
    "subscription.search_all": "Start immediate searches for all subscriptions accessible to the current user.",
    "subscription.share": "Publish one accessible subscription to the MoviePilot sharing service.",
    "subscription.share.delete": "Delete one shared-subscription publication by share ID.",
    "subscription.share.statistics": "Read aggregate contribution and reuse counts for subscription sharers.",
    "subscription.status.update": "Set one accessible subscription to running, paused, or stopped state.",
    "subscription.user.list": "List public subscriptions owned by one accessible MoviePilot username.",
    "subtitle.search.media": "Search subtitle providers for one canonical media identity and optional season or episode.",
    "subtitle.search.title": "Search subtitle providers from a free-form title and optional media filters.",
    "system.module.list": "List loaded MoviePilot module IDs and localized names.",
    "system.module.test": "Run the built-in availability test for one loaded MoviePilot module.",
    "system.network.targets": "List approved built-in network-test targets without exposing their request URLs.",
    "system.network.test": "Test connectivity to one approved target or the legacy constrained URL input.",
    "torrent.cache.clear": "Delete every cached torrent context.",
    "torrent.cache.delete": "Delete one cached torrent context by site domain and cache hash.",
    "torrent.cache.get": "Inspect cached torrent contexts and their recognized media identities.",
    "torrent.cache.refresh": "Refresh torrent caches from configured RSS or spider sources.",
    "torrent.cache.reidentify": "Replace or recompute the media identity for one cached torrent context.",
    "transfer.episode_format.recommend": "Recommend an episode-number extraction template from supplied file samples.",
    "transfer.history.clear": "Delete legacy transfer-history records while leaving files and durable failed-task records untouched.",
    "transfer.history.redo": "Start AI-assisted reorganization for one transfer-history record.",
    "transfer.history.redo_batch": "Start AI-assisted reorganization for an explicit list of transfer-history records.",
    "transfer.manual_history": "Check whether supplied storage items already have successful transfer history.",
    "transfer.manual_review": "Read one durable transfer task awaiting manual review.",
    "transfer.manual_review.resolve": "Record the authorized decision for one durable transfer manual-review operation.",
    "transfer.manual_reviews": "Page durable transfer tasks awaiting manual review or retry recovery.",
    "transfer.name": "Preview the organized destination name for one source path and media identity.",
    "transfer.queue": "List items waiting in the file-transfer queue.",
    "transfer.queue.delete": "Remove one exact storage item from the file-transfer queue and stop its transfer.",
    "transfer.target_path": "Resolve the configured transfer destination for supplied source storage items.",
    "workflow.actions": "List built-in workflow action definitions and their parameter contracts.",
    "workflow.create": "Create one workflow from a complete workflow definition.",
    "workflow.delete": "Delete one configured workflow by persistent workflow ID.",
    "workflow.event_types": "List event types that can trigger workflows.",
    "workflow.fork": "Create a local workflow from one shared workflow definition.",
    "workflow.get": "Read one complete configured workflow definition.",
    "workflow.pause": "Disable automatic execution of one configured workflow.",
    "workflow.plugin.actions": "List workflow actions contributed by installed plugins, optionally filtered by plugin ID.",
    "workflow.reset": "Reset one configured workflow definition and execution state.",
    "workflow.share": "Publish one configured workflow to the MoviePilot sharing service.",
    "workflow.share.delete": "Delete one shared-workflow publication by share ID.",
    "workflow.shares": "List shared workflows with name and pagination filters.",
    "workflow.start": "Enable automatic execution of one configured workflow.",
    "workflow.update": "Replace one configured workflow definition.",
    "config.user.get": "Read current-user feature flags, runtime capabilities, and effective permissions.",
    "config.public.get": "Read one explicitly public system setting by exact key.",
    "system.usage.statistics": "Read the installation version and runtime usage report available to the current user.",
    "plugin.folders.get": "Read the complete administrator plugin-folder grouping configuration.",
    "plugin.folders.update": "Replace the complete administrator plugin-folder grouping configuration.",
    "plugin.folder.create": "Create one named plugin folder.",
    "plugin.folder.update": "Incrementally rename one plugin folder or update its presentation settings.",
    "plugin.folder.delete": "Delete one named plugin folder without uninstalling its plugins.",
    "plugin.folder.plugins.update": "Replace the ordered plugin IDs assigned to one named plugin folder.",
    "plugin.folder.plugin.assign": "Move one installed plugin into one named folder and remove its other folder assignments.",
    "plugin.folder.plugin.remove": "Remove one installed plugin from one named folder without uninstalling it.",
}


FIELD_DESCRIPTIONS = {
    "action_name": "Exact action name whose capability contract should be returned.",
    "allow_unrecognized": "Allow a download when MoviePilot cannot resolve a canonical media identity.",
    "album": "Album title associated with a music recording or subscription.",
    "album_id": "Source-native album ID returned by music search, exploration, or artist-album browsing.",
    "album_type": "MusicBrainz release-group type filter: album, single, ep, broadcast, other, compilation, soundtrack, live, or remix.",
    "apikey": "Site API key used by sites that support API-key authentication.",
    "area": "Optional region filter applied by the torrent search workflow.",
    "artist_id": "Source-native artist ID returned by music search or an album detail response.",
    "audio_format": "Requested or recorded audio container or codec, such as FLAC or MP3.",
    "audio_lossless": "Whether the recorded audio result is lossless.",
    "audio_quality": "Subscription audio-quality rule, such as hires, lossless, or lossy.",
    "backdrop": "Backdrop image URL stored with the media or subscription.",
    "background": "Run the transfer asynchronously and return before completion.",
    "batch_id": "Stable subscription search batch identifier.",
    "basename": "Base filename without its parent path.",
    "best_version": "Enable normal best-version upgrading when set to 1.",
    "best_version_full": "Enable full best-version upgrading when set to 1.",
    "bit_depth": "Recorded audio bit depth in bits.",
    "bitrate": "Recorded audio bitrate in bits per second.",
    "body": (
        "Request value for the selected operation. Match the exact operation oneOf branch; "
        "scalar and object request bodies are not interchangeable."
    ),
    "category": "MoviePilot media category or filter-group category, depending on the operation.",
    "cache_key": "Exact recognition-cache key returned by music.cache.get.",
    "channel": "Message channel that originally submitted the download.",
    "children": "Child storage items nested below this item.",
    "code": "Two-factor verification code or site-specific authentication secret.",
    "command": "Complete slash command, including the leading slash and all arguments.",
    "completed_episode": "Highest episode number already completed for the subscription.",
    "current_site_id": "Configured site ID currently handling the subscription execution.",
    "cookie": "Site authentication cookie. Treat this value as a secret.",
    "count": "Maximum number of records to return on the requested page.",
    "current_audio_format": "Audio format of the best version currently held.",
    "current_bit_depth": "Bit depth of the best version currently held.",
    "current_bitrate": "Bitrate of the best version currently held.",
    "current_priority": "Calculated priority of the best version currently held.",
    "current_sample_rate": "Sample rate of the best version currently held.",
    "custom_words": "Custom recognition or rename words applied to this media workflow.",
    "classification_policy_revision": "Policy revision that produced the persisted classification snapshot.",
    "classification_rule_id": "Stable rule ID that selected the persisted classification category.",
    "classification_source": "Selection source recorded with the persisted classification snapshot.",
    "date": "Record creation or completion timestamp used by the history item.",
    "date_elapsed": "Human-readable age of the torrent publication date.",
    "days": "Recommendation time window in days.",
    "deletedest": "Also delete the organized destination files when deleting transfer history.",
    "deletesrc": "Also delete the recorded source files when deleting transfer history.",
    "description": "Human-readable media, torrent, or subscription description.",
    "dest": "Organized destination path recorded in transfer history.",
    "dest_fileitem": "Serialized destination storage item recorded by the transfer.",
    "dest_storage": "Configured storage name containing the organized destination.",
    "directory_type": "Directory configuration subtype to return.",
    "domain": "Site hostname or domain used for matching and requests.",
    "download_hash": "Provider-native torrent hash associated with the record.",
    "downloader": "Configured downloader instance name.",
    "downloadvolumefactor": "Torrent download-volume multiplier reported by the site.",
    "douban_sort": "Douban Music category order: U for comprehensive, S for rating, R for newest, or O for hottest.",
    "drive_id": "Provider-native storage drive identifier.",
    "effect": "Video or release-effect filter expression used by the subscription.",
    "error": "Human-readable workflow, provider, or execution error message.",
    "entity": "Music exploration entity: recording for tracks or album for release groups.",
    "enclosure": "Torrent download URL or enclosure supplied by the indexer result.",
    "episode_detail": "Episode mapping details used by manual transfer.",
    "episode_format": "Episode-number formatting rule used by manual transfer.",
    "episode_group": "TMDB episode-group identifier used for alternate episode ordering.",
    "episode_offset": "Integer offset added to detected episode numbers.",
    "episode_part": "Episode part number used when one episode is split across files.",
    "episode_priority": "Per-episode best-version priority state.",
    "episodes": "Episode-number expression recorded in history, such as E01-E03.",
    "errmsg": "Error message recorded for a failed transfer.",
    "expected_revision": "Exact current plugin source-identity revision returned by plugin.source.options.",
    "exclude": "Regular expression or filter expression that rejects matching releases.",
    "extension": "Filename extension, including or excluding the leading dot as returned by storage.",
    "fileid": "Provider-native storage item identifier.",
    "fileitem": "One complete source storage item returned by storage.list.",
    "fileitems": "Additional source storage items included in the same manual transfer.",
    "files": "Serialized list of files recorded by the history item.",
    "filter": "Named filter rule or rule expression applied to this site or subscription.",
    "filter_groups": "Ordered filter-rule group names applied to the subscription.",
    "force": "Force a marketplace refresh or plugin installation when true.",
    "freedate": "Torrent freeleech expiration timestamp reported by the site.",
    "freedate_diff": "Seconds remaining until the torrent freeleech period ends.",
    "fresh_sort": "Freshness ordering used by the recommendation source.",
    "from_begin": "Restart the workflow from its first action instead of resuming progress.",
    "from_history": "Treat the transfer input as originating from an existing history record.",
    "future": "Include future recommendation periods when supported.",
    "genre_id": "Genre identifier used to filter shared or popular subscriptions.",
    "grabs": "Number of completed downloads reported for the torrent.",
    "group": "Registered setting group used for dynamic setting discovery.",
    "hit_and_run": "Whether the torrent is subject to hit-and-run requirements.",
    "id": "Persistent database identifier of the supplied record.",
    "identifiers": "Complete ordered list of custom recognition identifier rules.",
    "image": "Image URL stored with the history record.",
    "include": "Regular expression or filter expression that a release must match.",
    "include_group_refs": "Include custom rules referenced only through rule groups.",
    "include_usage": "Include the subscriptions or defaults that reference each rule group.",
    "include_values": "Return complete setting values instead of discovery summaries.",
    "is_active": "Whether the configured site is enabled.",
    "jobid": "Exact scheduler job ID returned by scheduler.list.",
    "key": "Optional exact plugin data key used to narrow the returned preview.",
    "keyword": "Case-insensitive substring used to discover settings or filter storage entries.",
    "labels": "Torrent labels supplied by the site result.",
    "lack_episode": "Number of episodes still missing from the subscription.",
    "last_update": "Timestamp of the subscription's most recent update.",
    "library_category_folder": "Create or use a category-level folder in the target library.",
    "library_type_folder": "Create or use a media-type folder in the target library.",
    "limit_count": "Maximum number of site requests allowed in one rate-limit interval.",
    "limit_interval": "Number of requests in the site's rate-limit window.",
    "limit_seconds": "Site rate-limit window length in seconds.",
    "logid": "One download-history or transfer-log identifier used by manual transfer.",
    "logids": "Multiple download-history or transfer-log identifiers included in manual transfer.",
    "match_field": "Object field used to match one list item during an upsert or removal.",
    "match_value": "Exact value compared against match_field during a list-item update.",
    "max_chars": "Maximum number of serialized plugin-data characters to return.",
    "max_results": "Optional upper bound on plugin catalog results, from 1 to 200; omit it for the complete catalog.",
    "max_rating": "Maximum rating used to filter shared or popular subscriptions.",
    "media_category": "MoviePilot library category assigned to the media.",
    "media_category_id": "Stable classification category ID; preserve it separately from the current category path snapshot.",
    "media_id": "Source-native media ID. Always pair it with the exact media_source returned by search.",
    "media_source": "Metadata source identifier. Preserve the exact value returned with media_id.",
    "media_type": "MoviePilot media type used to filter recommendations or rule groups.",
    "min_bit_depth": "Minimum acceptable audio bit depth in bits.",
    "min_bitrate": "Minimum acceptable audio bitrate in bits per second.",
    "min_filesize": "Minimum source file size accepted by manual transfer, in bytes.",
    "min_listen_count": "Minimum listen count required for a music recommendation.",
    "min_rating": "Minimum rating used to filter shared or popular subscriptions.",
    "min_sample_rate": "Minimum acceptable audio sample rate in hertz.",
    "min_sub": "Minimum subscriber count used to filter popular subscriptions.",
    "mode": "Operation mode; music.explore accepts chart or fresh, while transfer history records move, copy, link, or softlink.",
    "modify_time": "Storage item modification timestamp.",
    "mtype": "MoviePilot media type or subscription-history category required by the operation.",
    "music_type": "Music identity level: recording, album, or artist where supported.",
    "name": "Human-readable name of the site, storage item, subscription, or rule group.",
    "new_name": "Replacement name for the existing filter-rule group.",
    "new_rule_id": "Replacement stable ID for the existing custom filter rule.",
    "note": "Structured auxiliary metadata stored with the record.",
    "operation": "Setting update mode: replace, merge_dict, upsert_list_item, or remove_list_item.",
    "operation_id": "Exact allowlisted MoviePilot operation ID selecting this oneOf branch.",
    "page": "One-based result page number.",
    "page_url": "Public details page for the torrent result.",
    "parent_fileid": "Provider-native identifier of the parent storage directory.",
    "password": "Site login password. Treat this value as a secret.",
    "past": "Include past recommendation periods when supported.",
    "path": "Storage or history path represented by this record.",
    "path_params": (
        "Resource identities inserted only into the selected operation's fixed route placeholders. "
        "Use the exact names and types in its oneOf branch."
    ),
    "peers": "Number of downloading peers reported for the torrent.",
    "person_id": "Source-native person ID returned by person search.",
    "pickcode": "115 storage pickcode associated with the item.",
    "plugin_id": "Exact installed or marketplace plugin ID.",
    "poster": "Poster image URL stored with the media or subscription.",
    "preview": "Validate and preview manual-transfer output without committing file changes.",
    "pri": "Site search priority; lower or higher ordering follows the existing site API convention.",
    "pri_order": "Indexer priority order assigned to the torrent result.",
    "proxy": "Whether the site uses MoviePilot's configured proxy.",
    "public": "Whether the site is treated as a public indexer.",
    "pubdate": "Torrent publication timestamp.",
    "publish_time": "Release-age filter expression for a custom filter rule.",
    "quality": "Video or release quality filter expression.",
    "query": (
        "Filters and control values sent in the query string. Use the exact names, types, "
        "defaults, and enums in the selected operation's oneOf branch."
    ),
    "range_name": "Named recommendation time range.",
    "release_version": "Exact plugin release version to install when one is required.",
    "remove_keys": "Object keys removed after merge_dict applies its supplied value.",
    "render": "Whether site requests require browser rendering.",
    "reorganize": "Allow manual transfer to organize an item that was already processed.",
    "repo_url": "Approved plugin repository URL used to resolve the installation source.",
    "resolution": "Video resolution filter expression, such as 1080p or 2160p.",
    "rss": "Site RSS feed URL.",
    "rule_id": "Stable custom filter-rule ID.",
    "rule_string": "Ordered filter-rule expression stored in the group.",
    "sample_rate": "Recorded audio sample rate in hertz.",
    "save_path": "Configured downloader-side save path for the download or subscription.",
    "scrape": "Generate metadata and images after manual transfer.",
    "search_imdbid": "Use IMDb identity during subscription search when set to 1.",
    "season": "Season number used by the media, search, subscription, or transfer operation.",
    "seasons": "Season-number expression recorded in history.",
    "seeders": "Minimum seeder expression for a filter rule, or the torrent's seeder count.",
    "server": "Exact configured media-server instance name returned by the media-server instance list.",
    "setting_key": "Exact registered setting key returned by config.system.get discovery.",
    "show_secrets": "Return unredacted secret values; use only with explicit authorization.",
    "site": "Source site identifier associated with the torrent result.",
    "site_cookie": "Site cookie bundled with the torrent result. Treat this value as a secret.",
    "site_downloader": "Downloader instance selected by the source site.",
    "site_id": "Persistent site ID returned by site.list.",
    "site_name": "Human-readable source site name.",
    "site_order": "Source site's configured search order.",
    "site_proxy": "Whether the torrent's source site uses the configured proxy.",
    "site_ua": "User-Agent associated with the source site.",
    "sites": "Exact site IDs included in the search or subscription scope.",
    "size": "File or torrent size in bytes.",
    "size_range": "Accepted torrent size range expression for a custom filter rule.",
    "sort": "Storage-list sort field or ordering expression.",
    "sort_by": "Recommendation field used for ordering results.",
    "sort_type": "Ascending or descending order used by shared or popular subscriptions.",
    "source": "Exact metadata or recommendation source selected by the operation.",
    "src": "Source path recorded in transfer history.",
    "src_fileitem": "Serialized source storage item recorded by the transfer.",
    "src_storage": "Configured storage name containing the transfer source.",
    "start_episode": "First episode number requested by the subscription.",
    "state": "Current site, subscription, marketplace, or transfer state filter.",
    "status": "Transfer success status used to filter history or describe a record.",
    "storage": "Configured storage name or storage type used by the operation.",
    "storage_type": "Configured storage provider type to return.",
    "stype": "Popular-subscription category requested by the endpoint.",
    "subscribe_id": "Persistent subscription ID returned by subscription.list.",
    "subtitle": "Optional subtitle text used together with title during media recognition.",
    "target_path": "Destination path used by manual transfer.",
    "target_storage": "Configured storage name receiving the manual transfer.",
    "tags": "Comma-separated Douban Music category tags; use only with a Douban Music exploration source.",
    "thumbnail": "Thumbnail URL returned by the storage provider.",
    "timeout": "Per-request site timeout in seconds.",
    "title": "Media, torrent, subscription, or history title used by the operation.",
    "tmdbid": "TMDB media ID returned by media search or detail.",
    "token": "Site authentication token. Treat this value as a secret.",
    "torrent_description": "Torrent release description recorded in download history.",
    "torrent_in": "Complete torrent candidate returned by search.results or search.torrents.",
    "torrent_name": "Torrent release name recorded in download history.",
    "torrent_site": "Source site name recorded in download history.",
    "total_episode": "Expected total episode count for the subscription.",
    "total_tracks": "Expected or recorded track count for a music item.",
    "transfer_task_id": "Stable durable transfer-task ID associated with the history record.",
    "transfer_type": "Manual-transfer mode, such as move, copy, link, or softlink.",
    "trigger_type": "Workflow trigger filter: timer, event, manual, or all.",
    "type": "MoviePilot media or storage item type required by the selected operation.",
    "type_name": "Explicit media type name used when source IDs alone are ambiguous.",
    "ua": "Site User-Agent string used for authenticated requests.",
    "uploadvolumefactor": "Torrent upload-volume multiplier reported by the site.",
    "url": "Site, storage, or torrent URL represented by this field.",
    "userid": "Message-channel user ID recorded with download history.",
    "username": "MoviePilot or site username required by the selected operation.",
    "value": "Complete replacement value, object fragment, or one list item for the selected update mode.",
    "volume_factor": "Combined upload/download volume-factor label shown for the torrent.",
    "vote": "Media vote average stored with the subscription.",
    "with_cover": "Require recommendation results to include cover artwork.",
    "workdate": "Date used when retrieving one site's historical user statistics.",
    "workflow_id": "Persistent workflow ID returned by workflow.list.",
    "year": "Release or premiere year used to disambiguate the media title.",
}

FIELD_DESCRIPTIONS.update(
    {
        "action": "Exact provider or workflow action identifier required by the selected operation.",
        "actions": "Ordered workflow action definitions executed by this workflow or flow.",
        "add_time": "Timestamp when the workflow definition was created.",
        "animated": "Whether the workflow connection is rendered as animated in the editor.",
        "attempt": "Current execution-attempt number for this workflow node.",
        "attempts": "Attempt counters keyed by workflow node or operation identity.",
        "backoff": "Retry backoff multiplier applied after each failed workflow action attempt.",
        "branch_policy": "Workflow branch policy controlling selected downstream paths.",
        "cat": "Exact site category identifier returned by site.category.",
        "check_only": "Validate or preview the recommendation without applying search-result filtering.",
        "can_cancel": "Whether the current subscription execution can be cancelled.",
        "concurrency_key": "Workflow expression used to serialize actions sharing the same runtime key.",
        "condition": "Workflow branch or flow condition expression evaluated at runtime.",
        "context": "Persisted workflow execution context available to later actions.",
        "current_action": "Identifier of the workflow action currently selected or executing.",
        "data": "Serialized workflow action configuration or runtime payload.",
        "decision": "Manual-review decision selected from the endpoint's declared enum.",
        "episode": "Episode number used to narrow a subtitle or media search.",
        "errors": "Workflow execution errors keyed or ordered by action identity.",
        "execution_status": "Current subscription execution status returned with the subscription snapshot.",
        "event_conditions": "Additional workflow event-filter conditions.",
        "event_type": "Exact event type returned by workflow.event_types.",
        "execution_config": "Workflow runtime limits, concurrency, and failure-policy configuration.",
        "execution_state": "Persisted resumable workflow execution state.",
        "fail_policy": "Workflow failure policy controlling stop, continue, or branch behavior.",
        "filetype": "Media file type used to preview the organized destination name.",
        "filtered_indices": "Zero-based search-result indices selected by the recommendation model.",
        "finished_actions": "Workflow action IDs already completed in the persisted execution state.",
        "finished_at": "Timestamp when the workflow node or execution finished.",
        "flows": "Workflow connection definitions linking action nodes.",
        "folder_name": "Exact plugin folder name returned by plugin.folders.get.",
        "genre_ids": "Genre identifiers accepted by the automatic category rule.",
        "history_id": "Persistent transfer- or subscription-history ID returned by a history operation.",
        "history_ids": "Explicit persistent transfer-history IDs included in one batch redo request.",
        "icon": "Icon name or URL used by a workflow, network target, plugin, or category.",
        "inputs": "Named input bindings consumed by this workflow action.",
        "interval": "Retry delay in seconds before the next workflow action attempt.",
        "job_id": "Exact scheduler job ID returned by scheduler.list.",
        "join_policy": "Workflow fan-in policy controlling when downstream execution may continue.",
        "last_time": "Timestamp of the workflow's most recent execution.",
        "max_attempts": "Maximum number of attempts allowed by the workflow retry policy.",
        "max_workers": "Maximum concurrent workflow actions allowed by the execution configuration.",
        "message": "Human-readable workflow runtime or provider result message.",
        "moduleid": "Exact loaded module ID returned by system.module.list.",
        "movie": "Automatic movie-category rules evaluated in order.",
        "node_states": "Persisted runtime states keyed by workflow node identity.",
        "nodes": "Persisted workflow node runtime states keyed by action identity.",
        "origin_country": "Production-country code matched by an automatic category rule.",
        "original_language": "Original-language code matched by an automatic category rule.",
        "outputs": "Named output mappings produced by this workflow action.",
        "page_size": "Maximum records returned on one page.",
        "phase": "Current phase of a subscription execution.",
        "params": "Provider-defined JSON parameters for the selected authentication or storage action.",
        "plugin_ids": "Exact plugin IDs whose aggregate ratings should be returned.",
        "position": "Workflow editor coordinates for one action node.",
        "production_countries": "Production-country codes matched by an automatic category rule.",
        "progress": "Current numeric or structured workflow execution progress.",
        "rating": "Numeric plugin rating accepted by the endpoint's declared bounds.",
        "reason": "Human-readable justification recorded with a manual-review decision.",
        "recursive": "Apply media-aware renaming recursively to child files when true.",
        "release_year": "Release year matched by an automatic category rule.",
        "result": "Persisted workflow action result value.",
        "result_payload": "Structured external-operation result recorded with manual review.",
        "retry": "Workflow retry-policy definition for this action.",
        "rulegroup_name": "Exact filter-rule group name returned by filter.groups.",
        "run_count": "Number of times the workflow has been executed.",
        "running_tasks": "Workflow task IDs currently executing.",
        "runtime": "Persisted workflow runtime metadata used for safe resume.",
        "share_comment": "Optional explanatory comment published with a shared item.",
        "share_id": "Persistent MoviePilot Server share ID returned by a share-list operation.",
        "share_title": "Public title used when publishing a subscription or workflow.",
        "share_uid": "Exact MoviePilot Server sharing-user ID to follow or unfollow.",
        "share_user": "Public contributor name used when publishing a subscription or workflow.",
        "site_url": "Configured site URL or hostname used to select one site's statistics.",
        "updated_at": "Timestamp when the subscription execution status was last updated.",
        "started_at": "Timestamp when the workflow node or execution started.",
        "subid": "Persistent subscription ID whose status or processing state will change.",
        "suffix": "File suffix or extension matched by an automatic category rule.",
        "target": "Exact target identifier selected by the operation.",
        "target_id": "Approved built-in network-test target ID returned by system.network.targets.",
        "task_id": "Stable durable transfer task ID returned by transfer.manual_reviews.",
        "timer": "Workflow timer or cron expression used for scheduled execution.",
        "torrent_hash": "Cache hash returned by torrent.cache.get for one exact site-domain entry.",
        "tv": "Automatic TV-category rules evaluated in order.",
        "version": "Plugin release or schema version selected by the operation.",
        "wiki_url": "Approved MoviePilot Wiki URL used as the plugin-market synchronization source.",
        "x": "Horizontal workflow editor coordinate.",
        "y": "Vertical workflow editor coordinate.",
    }
)


MODEL_DESCRIPTIONS = {
    "AgentCommandRunRequest": "Slash-command execution request.",
    "Body_add_api_v1_download_add_post": "MoviePilot download submission request.",
    "CategoryConfig": "Complete automatic media-category strategy configuration.",
    "CustomFilterRuleCreateRequest": "Custom filter-rule creation request.",
    "CustomFilterRuleUpdateRequest": "Custom filter-rule update request.",
    "CustomIdentifiersUpdateRequest": "Complete custom recognition-identifier replacement request.",
    "DownloadHistory-Input": "One MoviePilot download-history record.",
    "FileItem-Input": "One file or directory returned by a configured storage provider.",
    "FilterRuleGroupCreateRequest": "Filter-rule group creation request.",
    "FilterRuleGroupUpdateRequest": "Filter-rule group update request.",
    "JsonData-Input": "Arbitrary JSON-compatible auxiliary data.",
    "ManualTransferItem": "Manual file-transfer and organization request.",
    "MediaSource": "Canonical metadata source identifier paired with a source-native media ID.",
    "MediaType": "MoviePilot media type.",
    "MusicRecognizeRequest": "Exact source-native recording or album identity to resolve into canonical music metadata.",
    "PluginSourceChangeRequest": "Explicit online-source change request guarded by the current identity revision.",
    "PluginSourceInstallRequest": "Explicit online-source installation request for an unbound plugin.",
    "Site-Input": "Complete site configuration and runtime state.",
    "SiteCookieUpdate": "Site login request used to refresh the stored cookie and User-Agent.",
    "Subscribe": "Movie, TV, or music subscription input model.",
    "SystemSettingsUpdateRequest": "One registered system-setting update request.",
    "SystemUpdateRequest": "Selected MoviePilot application or site-resource update target.",
    "SubscriptionExecutionStatus": "Subscription refresh execution status and progress summary.",
    "TorrentInfo": "One torrent candidate returned by MoviePilot search.",
    "TransferHistory-Input": "One MoviePilot file-transfer history record.",
}

MODEL_DESCRIPTIONS.update(
    {
        "CategoryRule": "One ordered automatic media-category matching rule.",
        "PluginCloneRequest": "Plugin clone identifier and optional display-name request.",
        "PluginMarketSyncRequest": "Approved Wiki source request for plugin-market synchronization.",
        "PluginRatingRequest": "Current user's numeric plugin-rating submission.",
        "PluginFoldersData": "Complete mapping from plugin folder names to ordered plugin IDs or display configuration.",
        "PluginFolderConfigData": "One plugin folder's ordered members and optional presentation settings.",
        "PluginFolderUpdateRequest": "Incremental plugin-folder rename or presentation-settings update request.",
        "PluginFolderPluginsUpdateRequest": "Conditional replacement of one plugin folder's ordered members.",
        "Body_recommend_search_results_api_v1_search_recommend_post": "Torrent search results and recommendation controls supplied to the configured model.",
        "SiteAuth": "Supported site-account authentication provider and its exact parameter values.",
        "SitePriorityUpdate": "One configured site ID and its replacement search priority.",
        "ManageRequest": "Configured storage target, provider-defined action, and action parameters.",
        "SubscribeShare": "Shared subscription definition or publication metadata.",
        "EpisodeFormatRecommendItem": "File samples used to infer an episode-number extraction template.",
        "BatchTransferHistoryRedoRequest": "Explicit transfer-history IDs for AI-assisted batch reorganization.",
        "TransferManualReviewRequest": "Authorized decision and optional result for one durable transfer operation.",
        "Workflow-Input": "Complete workflow definition accepted by create and update operations.",
        "Action-Input": "One executable action node in a workflow definition.",
        "ActionPosition": "Editor coordinates for one workflow action node.",
        "ActionRetry": "Retry limits and timing for one workflow action.",
        "ActionFlow-Input": "One directed connection between workflow action nodes.",
        "WorkflowExecutionConfig": "Workflow concurrency, join, branch, and failure policies.",
        "WorkflowExecutionState-Input": "Persisted resumable workflow execution state.",
        "WorkflowNodeState": "Persisted runtime state for one workflow action node.",
        "WorkflowRuntimeState": "Complete persisted workflow runtime and progress state.",
        "WorkflowShare": "Shared workflow definition or publication metadata.",
    }
)


def _apply_field_descriptions(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Attach concrete English guidance without replacing richer endpoint text."""
    rewritten: dict[str, Any] = {}
    for key, value in schema.items():
        if isinstance(value, Mapping):
            rewritten[key] = _apply_field_descriptions(value)
        elif isinstance(value, list):
            rewritten[key] = [
                _apply_field_descriptions(item) if isinstance(item, Mapping) else deepcopy(item) for item in value
            ]
        else:
            rewritten[key] = deepcopy(value)
    properties = rewritten.get("properties")
    if isinstance(properties, dict):
        for field_name, field_schema in properties.items():
            if not isinstance(field_schema, dict):
                continue
            existing = field_schema.get("description")
            if isinstance(existing, str) and existing.strip() and not re.search(r"[\u3400-\u9fff]", existing):
                continue
            description = FIELD_DESCRIPTIONS.get(str(field_name))
            if not description:
                raise ValueError(f"MCP field guidance is missing: {field_name}")
            field_schema["description"] = description
    return rewritten


def _rewrite_schema_refs(
    schema: Mapping[str, Any],
    *,
    components: Mapping[str, Any],
    definitions: dict[str, Any],
) -> dict[str, Any]:
    """把 OpenAPI components 引用改写为独立 MCP schema 的 $defs 引用。"""
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
        name = reference.rsplit("/", 1)[-1]
        if name not in definitions:
            definitions[name] = {}
            source = components.get(name)
            if not isinstance(source, Mapping):
                raise ValueError(f"OpenAPI 缺少请求模型: {name}")
            definitions[name] = _rewrite_schema_refs(
                source,
                components=components,
                definitions=definitions,
            )
        return {"$ref": f"#/$defs/{name}"}

    rewritten: dict[str, Any] = {}
    for key, value in schema.items():
        if isinstance(value, Mapping):
            rewritten[key] = _rewrite_schema_refs(
                value,
                components=components,
                definitions=definitions,
            )
        elif isinstance(value, list):
            rewritten[key] = [
                _rewrite_schema_refs(
                    item,
                    components=components,
                    definitions=definitions,
                )
                if isinstance(item, Mapping)
                else deepcopy(item)
                for item in value
            ]
        else:
            rewritten[key] = deepcopy(value)
    return rewritten


def _parameter_object_schema(
    parameters: Sequence[Mapping[str, Any]],
    *,
    location: str,
    components: Mapping[str, Any],
    definitions: dict[str, Any],
) -> dict[str, Any] | None:
    """把 OpenAPI path/query 参数投影为网关结构化对象。"""
    selected = [parameter for parameter in parameters if parameter.get("in") == location]
    if not selected:
        return None
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in selected:
        name = str(parameter["name"])
        raw_schema = parameter.get("schema")
        if not isinstance(raw_schema, Mapping):
            raw_schema = {}
        field_schema = _rewrite_schema_refs(
            raw_schema,
            components=components,
            definitions=definitions,
        )
        if parameter.get("description") and "description" not in field_schema:
            field_schema["description"] = parameter["description"]
        properties[name] = field_schema
        if parameter.get("required"):
            required.append(name)
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    return result


def _request_body_schema(
    operation: Mapping[str, Any],
    *,
    components: Mapping[str, Any],
    definitions: dict[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    """读取一个 OpenAPI operation 的 JSON 请求体及必填性。"""
    request_body = operation.get("requestBody")
    if not isinstance(request_body, Mapping):
        return None, False
    content = request_body.get("content")
    if not isinstance(content, Mapping):
        return None, bool(request_body.get("required"))
    media = content.get("application/json")
    if not isinstance(media, Mapping):
        media = next((item for item in content.values() if isinstance(item, Mapping)), None)
    raw_schema = media.get("schema") if isinstance(media, Mapping) else None
    if not isinstance(raw_schema, Mapping):
        return None, bool(request_body.get("required"))
    return (
        _rewrite_schema_refs(
            raw_schema,
            components=components,
            definitions=definitions,
        ),
        bool(request_body.get("required")),
    )


def _person_credits_operation(openapi: Mapping[str, Any]) -> dict[str, Any]:
    """合并四个来源端点为稳定 media.person.credits 网关合同。"""
    paths = openapi.get("paths", {})
    source_paths = {
        "douban": "/api/v1/douban/person/credits/{person_id}",
        "tmdb": "/api/v1/tmdb/person/credits/{person_id}",
        "bangumi": "/api/v1/bangumi/person/credits/{person_id}",
        "anilist": "/api/v1/anilist/person/credits/{person_id}",
    }
    for source_path in source_paths.values():
        if source_path not in paths:
            raise ValueError(f"OpenAPI 缺少人物作品端点: {source_path}")
    representative = paths[source_paths["douban"]].get("get")
    responses = deepcopy(representative.get("responses")) if isinstance(representative, Mapping) else {}
    return {
        "summary": "Read person credits",
        "parameters": [
            {
                "name": "source",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": list(source_paths)},
                "description": "Metadata source that owns the person ID.",
            },
            {
                "name": "person_id",
                "in": "path",
                "required": True,
                "schema": {"type": "integer"},
                "description": "Source-native person ID.",
            },
            {
                "name": "page",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "minimum": 1, "default": 1},
            },
            {
                "name": "count",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "description": "Page size used by Bangumi and AniList; other sources ignore it.",
            },
        ],
        "responses": responses,
    }


def _resolve_openapi_operation(
    operation_id: str,
    route: Any,
    openapi: Mapping[str, Any],
) -> Mapping[str, Any]:
    """按固定路由读取 OpenAPI operation，并处理多来源合成路由。"""
    if operation_id == "media.person.credits":
        return _person_credits_operation(openapi)
    path_item = openapi.get("paths", {}).get(route.path)
    if not isinstance(path_item, Mapping):
        raise ValueError(f"OpenAPI 缺少 operation 路径: {operation_id} -> {route.path}")
    operation = path_item.get(str(route.method).lower())
    if not isinstance(operation, Mapping):
        raise ValueError(f"OpenAPI 缺少 operation 方法: {operation_id} -> {route.method} {route.path}")
    return operation


def _apply_operation_overrides(
    operation_id: str,
    query_schema: dict[str, Any] | None,
    body_schema: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, bool | None]:
    """补充同一路由多 operation 时无法由 FastAPI 自动表达的语义约束。"""
    if operation_id == "media.person.search" and query_schema is not None:
        query_schema["properties"]["type"] = {
            "type": "string",
            "const": "person",
            "description": "Literal person, selecting person search instead of media search.",
        }
        required = query_schema.setdefault("required", [])
        if "type" not in required:
            required.append("type")
    if operation_id in {"plugin.installed", "plugin.market"} and query_schema is not None:
        state = "installed" if operation_id == "plugin.installed" else "market"
        query_schema["properties"]["query"]["description"] = (
            "Optional case-insensitive keyword matched against plugin ID, name, description, and author."
        )
        query_schema["properties"]["state"] = {
            "type": "string",
            "const": state,
            "description": f"Literal {state}, selecting only {state} plugin catalog entries.",
        }
        required = query_schema.setdefault("required", [])
        if "state" not in required:
            required.append("state")
    if operation_id == "music.explore" and query_schema is not None:
        descriptions = {
            "media_source": (
                "Music exploration source. Use musicbrainz for chart/fresh modes or doubanmusic for tag browsing."
            ),
            "mode": "MusicBrainz mode: chart reads listening charts; fresh reads new album releases.",
            "entity": "Chart entity: recording for tracks or album for release groups. Fresh results are albums.",
            "range_name": "ListenBrainz chart range: this_week, this_month, this_year, week, month, or year.",
            "sort_by": "ListenBrainz chart order: listen_count.desc or listen_count.asc.",
            "sort": "Fresh-release order accepted by the current ListenBrainz implementation.",
            "days": "Fresh-release lookback/lookahead window, from 1 through the endpoint maximum.",
            "past": "Include releases before today in fresh mode.",
            "future": "Include releases after today in fresh mode.",
            "min_listen_count": "Minimum ListenBrainz listen count in chart mode.",
            "with_cover": "Keep only results with cover artwork when true.",
            "tags": "Comma-separated Douban Music tags used only when media_source is doubanmusic.",
            "douban_sort": "Douban Music order: U comprehensive, S rating, R newest, or O hottest.",
        }
        for field_name, description in descriptions.items():
            field_schema = query_schema["properties"].get(field_name)
            if isinstance(field_schema, dict):
                field_schema["description"] = description
    if operation_id == "plugin.config.update" and body_schema is not None:
        body_schema["minProperties"] = 1
        body_schema["description"] = (
            "Complete plugin configuration object. First call plugin.config.get, copy its returned model, "
            "change only the intended keys, and submit the full resulting object. Omit a key only when it "
            "must be removed. A successful update reinitializes the plugin and refreshes commands, jobs, and routes."
        )
    if operation_id == "system.upgrade.dev":
        return (
            {
                "type": "string",
                "const": "dev",
                "description": "Literal dev. Release updates must use the separate check, download, and install operations.",
            },
            True,
        )
    return body_schema, None


def _resolve_openapi_schema(
    schema: Mapping[str, Any],
    components: Mapping[str, Any],
) -> Mapping[str, Any]:
    """解析响应中使用的本地 OpenAPI 引用与可空联合。"""
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
        resolved = components.get(reference.rsplit("/", 1)[-1])
        if isinstance(resolved, Mapping):
            return _resolve_openapi_schema(resolved, components)
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        for alternative in alternatives:
            if not isinstance(alternative, Mapping):
                continue
            resolved = _resolve_openapi_schema(alternative, components)
            if resolved.get("type") != "null":
                return resolved
    return schema


def _response_data_schema(
    operation: Mapping[str, Any],
    components: Mapping[str, Any],
) -> Mapping[str, Any]:
    """读取统一响应的 data schema，供 MCP 标注集合输出合同。"""
    responses = operation.get("responses")
    if not isinstance(responses, Mapping):
        return {}
    success = responses.get("200") or responses.get(200)
    if not isinstance(success, Mapping):
        return {}
    content = success.get("content")
    if not isinstance(content, Mapping):
        return {}
    media = content.get("application/json")
    if not isinstance(media, Mapping):
        return {}
    raw_schema = media.get("schema")
    if not isinstance(raw_schema, Mapping):
        return {}
    response_schema = _resolve_openapi_schema(raw_schema, components)
    properties = response_schema.get("properties")
    if not isinstance(properties, Mapping):
        return response_schema
    data_schema = properties.get("data")
    if not isinstance(data_schema, Mapping):
        return response_schema
    return _resolve_openapi_schema(data_schema, components)


def _collection_response_contract(
    operation: Mapping[str, Any],
    components: Mapping[str, Any],
) -> dict[str, Any] | None:
    """从 OpenAPI 响应声明提取列表或结构化分页结果的机器可读合同。"""
    responses = operation.get("responses")
    success = responses.get("200") if isinstance(responses, Mapping) else None
    if not isinstance(success, Mapping) and isinstance(responses, Mapping):
        success = responses.get(200)
    headers = success.get("headers") if isinstance(success, Mapping) else None
    header_names = {str(name).lower() for name in headers} if isinstance(headers, Mapping) else set()
    data_schema = _response_data_schema(operation, components)
    if data_schema.get("type") == "array":
        has_total = "x-total-count" in header_names
        parameters = operation.get("parameters")
        query_parameters = (
            {
                str(parameter.get("name")): parameter
                for parameter in parameters
                if isinstance(parameter, Mapping) and parameter.get("in") == "query"
            }
            if isinstance(parameters, list)
            else {}
        )
        compatibility_parameters = [
            query_parameters.get("page"),
            query_parameters.get("count"),
        ]
        native_window_names = {"limit", "offset", "page_size", "max_results"}
        has_native_window_default = False
        for name in native_window_names:
            parameter = query_parameters.get(name)
            if not isinstance(parameter, Mapping):
                continue
            if parameter.get("required", False):
                has_native_window_default = True
                break
            schema = parameter.get("schema")
            if isinstance(schema, Mapping) and schema.get("default") is not None:
                has_native_window_default = True
                break
        defaults_to_unpaginated = (
            all(
                isinstance(parameter, Mapping)
                and not parameter.get("required", False)
                and isinstance(parameter.get("schema"), Mapping)
                and "default" not in parameter["schema"]
                for parameter in compatibility_parameters
            )
            and not has_native_window_default
        )
        return {
            "body_shape": "list",
            "result_count_field": "collection.result_count",
            "total_count_field": "collection.total_count" if has_total else None,
            "default_pagination": ("unpaginated" if defaults_to_unpaginated else "endpoint-defined"),
        }
    data_properties = data_schema.get("properties")
    if not isinstance(data_properties, Mapping) or "total" not in data_properties:
        return None
    items_field = next(
        (name for name in ("items", "list") if name in data_properties),
        None,
    )
    if items_field is None:
        return None
    return {
        "body_shape": "page_object",
        "items_field": f"data.{items_field}",
        "total_count_field": "data.total",
        "default_pagination": "endpoint-defined",
    }


def _collection_response_guidance(contract: Mapping[str, Any]) -> str:
    """把集合输出合同转换为 oneOf 分支中的英文自描述说明。"""
    if contract.get("body_shape") == "page_object":
        return (
            f" Collection response: items stay in {contract['items_field']} and the exact total "
            f"stays in {contract['total_count_field']}. For a count-only request, use the smallest "
            "documented page and read that total; do not query the database merely because item "
            "data is truncated."
        )
    if contract.get("total_count_field"):
        if contract.get("default_pagination") != "unpaginated":
            return (
                " Collection response: data remains a list and the endpoint's documented "
                "pagination or limit defaults remain in effect. Successful gateway output adds "
                "collection.result_count and the exact collection.total_count. For a count-only "
                "request, use the smallest valid page and read collection.total_count; do not "
                "query the database merely because item data is truncated."
            )
        return (
            " Collection response: data remains a list; omit both page and count to preserve the "
            "legacy complete result. Successful gateway output adds collection.result_count and "
            "the exact collection.total_count. For a count or summary, send page=1 and count=1, "
            "then read collection.total_count; do not query the database merely because item data "
            "is truncated."
        )
    return (
        " Collection response: data remains a list and successful gateway output adds "
        "collection.result_count. collection.total_count is omitted when the endpoint or its "
        "upstream source does not expose a total."
    )


def build_api_mcp_input_schema(
    *,
    openapi: Mapping[str, Any],
    routes: Mapping[str, Any],
    specs: Sequence[Any],
) -> dict[str, Any]:
    """构建全部白名单 operation 的完整 MCP oneOf 输入合同。"""
    components = openapi.get("components", {}).get("schemas", {})
    if not isinstance(components, Mapping):
        components = {}
    definitions: dict[str, Any] = {}
    spec_by_id = {spec.operation_id: spec for spec in specs}
    if set(spec_by_id) != set(routes):
        raise ValueError("API operation 策略与路由注册表不一致")

    branches: list[dict[str, Any]] = []
    for operation_id in sorted(routes):
        route = routes[operation_id]
        operation = _resolve_openapi_operation(operation_id, route, openapi)
        summary = OPERATION_DESCRIPTIONS.get(operation_id)
        if not summary:
            raise ValueError(f"MCP operation guidance is missing: {operation_id}")
        parameters = operation.get("parameters")
        if not isinstance(parameters, list):
            parameters = []
        path_schema = _parameter_object_schema(
            parameters,
            location="path",
            components=components,
            definitions=definitions,
        )
        query_schema = _parameter_object_schema(
            parameters,
            location="query",
            components=components,
            definitions=definitions,
        )
        body_schema, body_required = _request_body_schema(
            operation,
            components=components,
            definitions=definitions,
        )
        body_schema, required_override = _apply_operation_overrides(
            operation_id,
            query_schema,
            body_schema,
        )
        if required_override is not None:
            body_required = required_override

        properties: dict[str, Any] = {
            "operation_id": {"type": "string", "const": operation_id},
        }
        required = ["operation_id"]
        if path_schema is not None:
            path_schema["description"] = (
                f"Resource identity placeholders for {operation_id}. {summary} Use only the named fields below."
            )
            properties["path_params"] = path_schema
            if path_schema.get("required"):
                required.append("path_params")
        if query_schema is not None:
            query_schema["description"] = (
                f"Filters and control values for {operation_id}. {summary} Use only the named fields below."
            )
            properties["query"] = query_schema
            if query_schema.get("required"):
                required.append("query")
        if body_schema is not None:
            body_schema.setdefault(
                "description",
                f"Request value for {operation_id}. {summary} Use the exact type and fields below.",
            )
            properties["body"] = body_schema
            if body_required:
                required.append("body")

        spec = spec_by_id[operation_id]
        collection_contract = _collection_response_contract(operation, components)
        collection_guidance = (
            _collection_response_guidance(collection_contract) if collection_contract is not None else ""
        )
        branch = {
            "type": "object",
            "title": operation_id,
            "description": (
                f"{summary} Method: {route.method}. Path: {route.path}. "
                f"Effect: {spec.effect.value}.{collection_guidance}"
            ),
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        if collection_contract is not None:
            branch["x-moviepilot-collection"] = collection_contract
        branches.append(branch)

    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "moviepilot_api",
        "type": "object",
        "description": (
            "Select the oneOf branch matching operation_id and send exactly its documented fields. "
            "Collection branches also describe their additive output metadata in "
            "x-moviepilot-collection."
        ),
        "properties": {
            "operation_id": {"type": "string", "enum": sorted(routes)},
            "path_params": {"type": "object"},
            "query": {"type": "object"},
            "body": {},
        },
        "required": ["operation_id"],
        "oneOf": branches,
    }
    if definitions:
        for model_name, definition in definitions.items():
            description = MODEL_DESCRIPTIONS.get(model_name)
            if not description:
                raise ValueError(f"MCP model guidance is missing: {model_name}")
            definition["description"] = description
        schema["$defs"] = definitions
    return _apply_field_descriptions(schema)


__all__ = ["build_api_mcp_input_schema"]
