# Filter APIs

Built-in and custom filter rules, groups, and rule testing.

## Operations

### `filter.builtin`
`GET /api/v1/rule/builtin`; policy effect: `safe_read`.
Purpose: List built-in torrent filter rules.
- `path_params`: none
- `query`: `rule_ids` (array<string>|null): Exact built-in rule IDs to return. Repeat rule_ids in the query string; omit it to list every built-in rule.
- `body`: none

### `filter.custom`
`GET /api/v1/rule/custom`; policy effect: `safe_read`.
Purpose: List user-defined torrent filter rules.
- `path_params`: none
- `query`: `include_group_refs` (boolean; default `True`): Include custom rules referenced only through rule groups.; `rule_ids` (array<string>|null): Exact custom rule IDs to return. Repeat rule_ids in the query string; omit it to list every custom rule.
- `body`: none

### `filter.custom.add`
`POST /api/v1/rule/custom`; policy effect: `reversible_write`.
Purpose: Create one user-defined torrent filter rule.
- `path_params`: none
- `query`: none
- `body`: `exclude` (string|null): Regular expression or filter expression that rejects matching releases.; `include` (string|null): Regular expression or filter expression that a release must match.; `name*` (string): Human-readable name of the site, storage item, subscription, or rule group.; `publish_time` (string|null): Release-age filter expression for a custom filter rule.; `rule_id*` (string): Stable custom filter-rule ID.; `seeders` (string|null): Minimum seeder expression for a filter rule, or the torrent's seeder count.; `size_range` (string|null): Accepted torrent size range expression for a custom filter rule.

### `filter.custom.delete`
`DELETE /api/v1/rule/custom/{rule_id}`; policy effect: `destructive_write`.
Purpose: Delete one user-defined torrent filter rule.
- `path_params`: `rule_id*` (string): Stable custom filter-rule ID.
- `query`: none
- `body`: none

### `filter.custom.update`
`PUT /api/v1/rule/custom/{rule_id}`; policy effect: `reversible_write`.
Purpose: Update one user-defined torrent filter rule.
- `path_params`: `rule_id*` (string): Stable custom filter-rule ID.
- `query`: none
- `body`: `exclude` (string|null): Regular expression or filter expression that rejects matching releases.; `include` (string|null): Regular expression or filter expression that a release must match.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `new_rule_id` (string|null): Replacement stable ID for the existing custom filter rule.; `publish_time` (string|null): Release-age filter expression for a custom filter rule.; `seeders` (string|null): Minimum seeder expression for a filter rule, or the torrent's seeder count.; `size_range` (string|null): Accepted torrent size range expression for a custom filter rule.

### `filter.group.add`
`POST /api/v1/rule/groups`; policy effect: `reversible_write`.
Purpose: Create one named filter-rule group.
- `path_params`: none
- `query`: none
- `body`: `category` (string|null): MoviePilot media category or filter-group category, depending on the operation.; `media_type` (string|null): MoviePilot media type used to filter recommendations or rule groups.; `name*` (string): Human-readable name of the site, storage item, subscription, or rule group.; `rule_string*` (string): Ordered filter-rule expression stored in the group.

### `filter.group.delete`
`DELETE /api/v1/rule/groups/{name}`; policy effect: `destructive_write`.
Purpose: Delete one named filter-rule group.
- `path_params`: `name*` (string): Human-readable name of the site, storage item, subscription, or rule group.
- `query`: none
- `body`: none

### `filter.group.update`
`PUT /api/v1/rule/groups/{name}`; policy effect: `reversible_write`.
Purpose: Update or rename one named filter-rule group.
- `path_params`: `name*` (string): Human-readable name of the site, storage item, subscription, or rule group.
- `query`: none
- `body`: `category` (string|null): MoviePilot media category or filter-group category, depending on the operation.; `media_type` (string|null): MoviePilot media type used to filter recommendations or rule groups.; `new_name` (string|null): Replacement name for the existing filter-rule group.; `rule_string` (string|null): Ordered filter-rule expression stored in the group.

### `filter.groups`
`GET /api/v1/rule/groups`; policy effect: `safe_read`.
Purpose: List named filter-rule groups.
- `path_params`: none
- `query`: `group_names` (array<string>|null): Exact rule-group names to return. Repeat group_names in the query string; omit it to list every group.; `include_usage` (boolean; default `True`): Include the subscriptions or defaults that reference each rule group.
- `body`: none

### `filter.test`
`GET /api/v1/system/ruletest`; policy effect: `external_side_effect`.
Purpose: Test one title and optional subtitle against an exact named filter-rule group.
- `path_params`: none
- `query`: `rulegroup_name*` (string): Exact filter-rule group name returned by filter.groups.; `subtitle` (string|null): Optional subtitle text used together with title during media recognition.; `title*` (string): Media, torrent, subscription, or history title used by the operation.
- `body`: none
