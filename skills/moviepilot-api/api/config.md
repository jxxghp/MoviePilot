# Configuration APIs

Configuration discovery, identifiers, user settings, and system setting updates.

## Operations

### `config.identifiers.get`
`GET /api/v1/system/identifiers`; policy effect: `safe_read`.
Purpose: Read the complete custom media-recognition identifier list.
- `path_params`: none
- `query`: none
- `body`: none

### `config.identifiers.update`
`POST /api/v1/system/identifiers`; policy effect: `reversible_write`.
Purpose: Replace the complete custom media-recognition identifier list.
- `path_params`: none
- `query`: none
- `body`: `expected_identifiers` (array<string>|null): Previously read complete ordered list. When supplied, reject the replacement if the stored list has changed.; `identifiers` (array<string>): Complete ordered list of custom recognition identifier rules.

### `config.public.get`
`GET /api/v1/system/setting/public/{key}`; policy effect: `safe_read`.
Purpose: Read one explicitly public system setting by exact key.
- `path_params`: `key*` (string): Optional exact plugin data key used to narrow the returned preview.
- `query`: none
- `body`: none

### `config.system.get`
`GET /api/v1/system/settings`; policy effect: `safe_read`.
Purpose: Discover registered system settings or read one exact setting.
- `path_params`: none
- `query`: `group` (string|null; default `all`): Discovery group used when setting_key is omitted. Supported groups are all, settings, systemconfig, downloaders, media_servers, notifications, notification_switches, storages, directories, search_sites, subscribe_sites, site_auth, ai_agent, filter_rules, subscribe_defaults, plugins, customization, transfer, scraping, and misc.; `include_values` (boolean|null): Return full values. Defaults to true for one exact key and false for discovery results.; `keyword` (string|null): Case-insensitive substring used to discover matching keys, groups, or labels.; `setting_key` (string|null): Exact setting key. Accepts Settings field names such as APP_DOMAIN or LLM_MODEL, SystemConfigKey values or enum names such as Downloaders or MediaServers, and aliases that resolve to one unique setting. Omit it to discover settings.; `show_secrets` (boolean; default `False`): Return unredacted secret values. Defaults to false and remains confirmation-protected.
- `body`: none

### `config.system.update`
`POST /api/v1/system/settings`; policy effect: `reversible_write`.
Purpose: Update one exact registered system setting.
- `path_params`: none
- `query`: none
- `body`: `match_field` (string|null): Object field used to match a list item. Downloaders, MediaServers, Notifications, Directories, and Storages default to name; NotificationSwitchs defaults to type. Supply it for other object lists.; `match_value` (value): Value compared against match_field. If omitted, use value[match_field]; scalar lists use value directly.; `operation` (string(replace,merge_dict,upsert_list_item,remove_list_item); default `replace`): replace overwrites the complete value; merge_dict shallow-merges an object; upsert_list_item inserts or replaces one matched list item; remove_list_item removes one matched list item.; `remove_keys` (array<string>): Object keys to remove after merge_dict applies the supplied value.; `setting_key*` (string): Exact setting key. Accepts a Settings field name, a SystemConfigKey value or enum name, or an alias that resolves to one unique setting. Call config.system.get with group or keyword first when the key is unknown.; `value` (value): New value or list item. For replace, send the complete value. For merge_dict, send the object fragment to merge. For upsert_list_item or remove_list_item, send one object or scalar item.

### `config.user.get`
`GET /api/v1/system/global/user`; policy effect: `safe_read`.
Purpose: Read current-user feature flags, runtime capabilities, and effective permissions.
- `path_params`: none
- `query`: none
- `body`: none

## System Settings Contract

Do not enumerate setting keys in this Skill. Settings change as MoviePilot evolves, so use `config.system.get` as the runtime discovery operation before updating an unfamiliar key.

| `source` | Contents | Persistence |
| :--- | :--- | :--- |
| `settings` | Runtime `Settings` fields such as APP_DOMAIN or LLM_MODEL | Type-converted and persisted to `app.env`, then applied to the current process |
| `systemconfig` | Database-backed business configuration such as downloaders, media servers, directories, and notifications | Written through the configuration service with plugin admission and change events |

The `systemconfig` database table is only the physical store for the second source. Use `config.system.get/update` for normal reads and writes. Direct SQL is reserved for an explicitly authorized repair when the managed API cannot complete the operation.

### Discover definitions

1. Call `config.system.get` with `query={"group":"settings","keyword":"LLM"}` or another group/keyword. Discovery defaults to summaries instead of full values.
2. Each returned setting includes `setting_key`, `source`, `group`, `label`, and a `definition` object with `declared_type`, current `value_shape`, `nullable`, `sensitive`, allowed `update_operations`, `default_match_field`, and `persistence`.
3. Read one exact value with `query={"setting_key":"LLM_MODEL"}`. Exact-key reads include the value by default.
4. Use `show_secrets=true` only when an administrator explicitly requests the plaintext value; secret reads remain confirmation-protected.

### Update settings

Choose an operation listed in the discovered setting definition, then send it in `body`:

| operation | Fields | Meaning |
| :--- | :--- | :--- |
| `replace` | `setting_key*`, `value` | Replace the complete scalar, list, or object value |
| `merge_dict` | `setting_key*`, `value`; optional `remove_keys` | Shallow-merge an object and optionally remove keys |
| `upsert_list_item` | `setting_key*`, `value`; optional `match_field`, `match_value` | Replace a matched list item or append it when absent |
| `remove_list_item` | `setting_key*`, `value` or `match_value`; optional `match_field` | Remove one matched list item without replacing the list |

After every update, call `config.system.get` again with the exact setting_key and verify the saved value. Do not guess a key, value shape, list match field, or update operation when discovery can return it.
