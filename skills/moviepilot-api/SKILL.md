---
name: moviepilot-api
version: 16
description: >-
  Use this skill for MoviePilot product operations such as media search, torrent
  search, downloads, subscriptions, library checks, sites, storage, workflows,
  schedulers, plugins, filter rules, and system settings. It authorizes the
  structured moviepilot_api gateway only; it does not authorize arbitrary HTTP,
  legacy Agent tools, MCP compatibility commands, authentication headers, or API
  tokens.
allowed-tools: moviepilot_api
allowed-api-operations: >-
  media.search media.person.search media.person.credits media.recognize media.scrape
  media.episode_schedule media.detail subscription.add subscription.update
  subscription.search subscription.list subscription.shares subscription.popular
  subscription.history subscription.delete download.add download.history.delete
  transfer.history.delete site.list site.update site.userdata site.test site.cookie.update
  recommendation.list library.exists
  storage.settings storage.list transfer.history transfer.file scheduler.list scheduler.run
  workflow.list workflow.run plugin.installed plugin.market plugin.capabilities
  plugin.config.get plugin.config.update plugin.reload plugin.install plugin.uninstall
  slash.list config.identifiers.get config.identifiers.update search.torrents search.results
  filter.builtin filter.custom filter.groups filter.custom.add
  filter.custom.update filter.custom.delete filter.group.add filter.group.update
  filter.group.delete plugin.data config.system.get config.system.update slash.run
---

# MoviePilot API

Use `moviepilot_api` for normal MoviePilot business operations. The tool accepts
only `operation_id`, `path_params`, `query`, and `body`. The host chooses the
fixed HTTP method and path, creates the current user's authentication token,
applies authorization and confirmation policy, and returns the API response.
When the gateway is called directly, the host automatically loads this Skill's
operation scope before enforcing the allowlist; explicitly loading the Skill is
still preferred so the model receives the full parameter and failure-handling
contract before it calls the gateway.

Never provide a URL, method, authentication header, API key, or access token.
Never fall back to a retired tool name or `moviepilot tool` MCP command. If an
operation is not listed in this skill, do not simulate it through arbitrary HTTP;
use a more specific skill or explain that the structured operation is unavailable.
Use `downloader-operation` for downloader instances, task inspection and native
task control. Use `mediaserver-operation` for libraries, items, playback sessions,
scans, refreshes and other native media-server capabilities.

## Calling Contract

Call the gateway with this shape:

```json
{
  "operation_id": "media.search",
  "path_params": {},
  "query": {"title": "流浪地球", "type": "media"},
  "body": {}
}
```

- Put route placeholders such as `subscribe_id`, `hashString`, `plugin_id`,
  `workflow_id`, `media_id`, `storage`, `rule_id`, and `name` in `path_params`.
- Put GET filters and control values in `query`. The gateway also accepts GET
  values in `body`, but use `query` consistently except for the protected secret
  flow below.
- Put POST, PUT, and PATCH request models in `body`.
- Preserve the exact source-native `media_source` + `media_id` returned by a
  search or detail response. For music, also preserve
  `music_type=recording|album|artist`; an artist is browse-only.
- Treat `success=false`, HTTP error data, empty results, and validation errors as
  real outcomes. Do not claim success without checking the response.

## Operation Catalog

### Media and search

| Operation | Parameters | Purpose |
|---|---|---|
| `media.search` | query: `title`, `type`, `page`, `count`, `media_source` | Search video, music, collection, or media entities |
| `media.person.search` | query: `title`, `type=person`, paging/source | Search people |
| `media.person.credits` | path: `source`, `person_id`; query: paging | Read TMDB or Douban credits |
| `media.recognize` | query: `title`, optional `subtitle`, `custom_words`, `media_source` | Recognize a title or file path |
| `media.detail` | path: `media_id`; query: `media_source`, `type_name`, music fields when applicable | Read exact media detail |
| `media.episode_schedule` | path: `tmdbid`, `season`; query: optional episode group | Read TMDB season episodes |
| `recommendation.list` | query: source/category/paging fields | Read the unified recommendation feed |
| `search.torrents` | path: `media_id`; query: `media_source`, `mtype`, `season`, `sites`, `music_type` | Search site resources |
| `search.results` | query: result filters | Read and filter the latest search context |
| `media.scrape` | path: `storage`; query: identity/type; body: file item | Scrape metadata, artwork, and configured music lyrics |

After `search.torrents`, present the returned filter choices before narrowing
results. Reuse `search.results` instead of repeating the same search. Obtain
explicit consent before `download.add` or another external side effect.

### Subscriptions and downloads

| Operation | Parameters | Purpose |
|---|---|---|
| `subscription.list` | query filters | List subscriptions |
| `subscription.add` | body: subscribe model | Create a subscription |
| `subscription.update` | body: updated subscribe model | Update a subscription |
| `subscription.search` | path: `subscribe_id` | Trigger/search one subscription |
| `subscription.delete` | path: `subscribe_id` | Permanently remove a subscription |
| `subscription.history` | path: `mtype`; query paging | Read subscription history |
| `subscription.shares` | query paging | Read shared subscriptions |
| `subscription.popular` | query paging/type | Read popular subscriptions |
| `download.add` | body: torrent input and optional media identity/client/path | Add a download |
| `download.history.delete` | query/body accepted by endpoint | Delete download history |

Before adding a download or subscription, check `library.exists` and
`subscription.list` when duplicate risk exists. Deletions and file removal need
explicit confirmation.

### Library, storage, and transfer

| Operation | Parameters | Purpose |
|---|---|---|
| `library.exists` | query: exact media identity and type | Check library presence |
| `storage.settings` | none | Read configured download/library storage roots |
| `storage.list` | body: storage/path/paging/sort fields | List a local or remote storage directory |
| `transfer.history` | query filters and paging | Read transfer history |
| `transfer.file` | body: manual-transfer model | Organize a file or directory |
| `transfer.history.delete` | query/body accepted by endpoint | Submit durable retry or delete legacy history |

For transfer retries, preserve durable scheduler evidence. If history deletion
returns a durable retry decision, stop and report it; only an actually deleted
legacy record may be followed by `transfer.file`.

Downloader task state and media-server library browsing deliberately do not pass
through this gateway. Discover the configured instance and its live capability
set with the matching provider-operation skill before calling the fixed helper.

### Sites, workflows, and schedulers

| Operation | Parameters | Purpose |
|---|---|---|
| `site.list` | query filters | List configured sites |
| `site.userdata` | path: `site_id` | Read site account data |
| `site.test` | path: `site_id` | Test site connectivity/login |
| `site.update` | body: site model | Update site configuration |
| `site.cookie.update` | path: `site_id`; body: credentials/2FA fields | Refresh site authentication |
| `scheduler.list` | none | List system/plugin/workflow schedules |
| `scheduler.run` | query: `job_id` | Run one scheduler job |
| `workflow.list` | query filters | List workflows |
| `workflow.run` | path: `workflow_id`; body/query endpoint fields | Run one workflow |

Scheduler `job_id` values are strings and are unrelated to autonomous Agent
task IDs. Test and credential updates are external side effects and require the
appropriate authorization/confirmation.

### Plugins, rules, and configuration

| Operation | Parameters | Purpose |
|---|---|---|
| `plugin.installed` / `plugin.market` | query filters | List installed or market plugins |
| `plugin.capabilities` | query: optional plugin ID | Read commands, actions, services, and Agent capabilities |
| `plugin.config.get` / `plugin.config.update` | path: `plugin_id`; update body | Read or update plugin configuration |
| `plugin.data` | path: `plugin_id`; query: key/limit/offset | Read bounded plugin data previews |
| `plugin.install` / `plugin.reload` / `plugin.uninstall` | path: `plugin_id` | Manage plugin lifecycle |
| `filter.builtin` / `filter.custom` / `filter.groups` | none or query filters | Read filter definitions |
| `filter.custom.add` / `filter.custom.update` / `filter.custom.delete` | update/delete path: `rule_id`; body for writes | Manage custom filter rules |
| `filter.group.add` / `filter.group.update` / `filter.group.delete` | update/delete path: `name`; body for writes | Manage filter groups |
| `config.identifiers.get` / `config.identifiers.update` | update body | Read or replace custom identifiers |
| `config.system.get` / `config.system.update` | query/body for read; update body | Read or update system settings |
| `slash.list` / `slash.run` | run body: `command` | Discover or dispatch system/plugin slash commands |

For a raw credential explicitly requested by an administrator, call
`config.system.get` with `body.show_secrets=true` and the narrowest
`body.setting_key` or `body.group`. The host pauses the turn for confirmation and
delivers the value through a protected channel. Never repeat the secret in a
normal response.

Plugin install, reload, uninstall, configuration writes, rule writes, system
setting writes, identifier writes, scheduler/workflow runs, and slash commands
are state changes. Inspect current state first and confirm unless the user's
request already explicitly authorizes the exact action.
