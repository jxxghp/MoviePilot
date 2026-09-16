---
name: publish-moviepilot-plugin
version: 3
description: >-
  Use this skill when the user asks to publish, upload, sync, pull, push, diff,
  or maintain a MoviePilot local plugin in a GitHub repository. Covers using the
  configured MoviePilot GitHub token, PLUGIN_LOCAL_REPO_PATHS local plugin
  repositories, package.json/package.v2.json metadata, plugins/plugins.v2
  layouts, safe file exclusion, diff preview before publishing, incremental
  GitHub Contents API updates, and syncing local plugin changes back from GitHub.
  Includes asking whether to use an existing repository or create a new public
  repository when no target repository is available.
  Also use for Chinese requests mentioning 插件发布, 插件维护, 推送插件到 GitHub,
  从 GitHub 拉取插件, 同步本地插件仓库, 增量发布插件, 插件仓库维护.
allowed-tools: read_file write_file edit_file apply_patch execute_command moviepilot_api
allowed-api-operations: config.system.get config.system.update
---

# Publish MoviePilot Plugin

Use this skill to publish and maintain a MoviePilot local plugin repository
through GitHub while protecting local secrets and unrelated plugins.

## Scope

- Publish one local plugin under `plugins.v2/<plugin_id_lower>/` or
  `plugins/<plugin_id_lower>/` to a GitHub repository.
- Merge only that plugin's entry into `package.v2.json` or `package.json`.
- Preview local/remote differences before writing.
- Pull remote plugin files back to the local plugin source.
- Create the target GitHub repository when the user explicitly chooses automatic
  creation; repositories are public by default unless the user asks for private.
- Reuse MoviePilot settings `GITHUB_TOKEN`, `REPO_GITHUB_TOKEN`,
  and `PLUGIN_LOCAL_REPO_PATHS` when available.

## Ground Truth

- Local plugin development rules: `skills/create-moviepilot-plugin/SKILL.md`.
- Local plugin source discovery: `app/adapters/external/market.py`,
  `PluginHelper.get_local_repo_paths()`.
- GitHub token settings: `app/runtime/config.py`, especially `GITHUB_TOKEN` and
  `REPO_GITHUB_TOKEN`.
- Plugin package layouts:
  - V2: `package.v2.json` and `plugins.v2/<plugin_id_lower>/`
  - Legacy: `package.json` and `plugins/<plugin_id_lower>/`

## Pre-Flight

1. Identify the target plugin ID and local source repository.
   - If the user gives a path, use it.
   - Otherwise query `PLUGIN_LOCAL_REPO_PATHS`; if exactly one configured
     repository contains the plugin, use it.
   - If several configured repositories contain the plugin, ask which one.
2. Identify the GitHub repository as `owner/repo`.
   - Use the user's explicit repository first.
   - If omitted, infer only when the local source has an obvious Git remote.
   - If neither is available, ask whether to use an existing repository or
     automatically create a new public repository.
   - If the user chooses an existing repository, ask for `owner/repo`.
   - If the user chooses automatic creation, ask for the target `owner/repo`
     and state that the repository will be public by default.
   - Do not create a private repository unless the user explicitly asks for it.
3. Select the package version layout.
   - Prefer `v2` when `package.v2.json` or `plugins.v2/<plugin_id_lower>/`
     exists.
   - Use legacy only when the local plugin is under `plugins/`.
4. Verify token availability.
   - Prefer `REPO_GITHUB_TOKEN` for the target repo when configured.
   - Fall back to `GITHUB_TOKEN`.
   - If no token is configured, ask the user to configure one before pushing.
     Read-only preview may still run without a token for public repositories.

## Script

Use `scripts/publish_plugin.py` for deterministic GitHub operations.

```bash
python skills/publish-moviepilot-plugin/scripts/publish_plugin.py preview \
  --repo owner/repo \
  --plugin-id MyPlugin \
  --local-repo /path/to/MoviePilot-Plugins \
  --package-version v2

python skills/publish-moviepilot-plugin/scripts/publish_plugin.py push \
  --repo owner/repo \
  --plugin-id MyPlugin \
  --local-repo /path/to/MoviePilot-Plugins \
  --package-version v2 \
  --message "Publish MyPlugin v1.0.0"

python skills/publish-moviepilot-plugin/scripts/publish_plugin.py pull \
  --repo owner/repo \
  --plugin-id MyPlugin \
  --local-repo /path/to/MoviePilot-Plugins \
  --package-version v2

python skills/publish-moviepilot-plugin/scripts/publish_plugin.py create-repo \
  --repo owner/repo
```

Options:

- `create-repo`: create the target GitHub repository. Default visibility is
  public; use `--private` only when the user explicitly asked for private.
- `preview`: compare local filtered files with remote files and print JSON.
- `push`: upload changed files and merge the plugin package entry.
- `pull`: write remote plugin files and package entry into local source.
- `--create-repo-if-missing`: on push, create the target public repository when
  GitHub reports that it does not exist.
- `--delete-remote`: on push, delete remote plugin files that no longer exist
  locally after exclusions.
- `--force`: on pull, allow overwriting local files that differ from remote.
- `--include PATTERN`: add files otherwise excluded by default.
- `--exclude PATTERN`: add an extra ignore pattern.
- `--dry-run`: print planned changes without writing.
- `--proxy URL`: use an explicit HTTP/HTTPS proxy for GitHub API requests.

## Safety Rules

- Always run `preview` before `push` unless the user explicitly asks for a
  direct push and already reviewed the diff.
- When no repository is known, ask the user to choose:
  `使用已有 GitHub 仓库` or `自动创建 GitHub 仓库（默认 public）`.
- Only run `create-repo` or `push --create-repo-if-missing` after the user has
  explicitly chosen automatic creation.
- Never upload these files unless explicitly included:
  `.env`, `.env.*`, `config/`, `data/`, `cache/`, `logs/`, `tmp/`,
  `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`,
  `.DS_Store`, `*.pyc`, `*.pyo`, `*.db`, `*.sqlite`, `*.sqlite3`, `*.log`,
  `*.bak`, `*.tmp`, `*.secret`, `*.key`, `*.pem`, `*.crt`, `*.p12`, `*.pfx`,
  `node_modules/`.
- For Vue federation plugins, publish built runtime assets under `dist/assets/`
  when they are present; do not exclude them as generated files.
- Do not overwrite or remove package entries for other plugins.
- Do not log or print GitHub token values.
- For push operations, report created, updated, deleted, skipped, and rejected
  files separately.
- For pull operations, preserve local-only ignored files and refuse to overwrite
  differing local files unless `--force` is used.

## Examples

User asks: `把本地 MyPlugin 发布到我的 GitHub 插件仓库`

1. Find `MyPlugin` under configured `PLUGIN_LOCAL_REPO_PATHS`.
2. Ask whether to use an existing repository or create a new public repository
   if `owner/repo` cannot be inferred.
3. Run `preview` and summarize the diff.
4. Run `push` only after the user confirms or requested immediate publish.

User asks: `发布插件，没有 GitHub 仓库`

1. Ask for the target `owner/repo` and confirm automatic creation.
2. Run `create-repo` or use `push --create-repo-if-missing`.
3. Continue with `preview` and `push` after repository creation succeeds.

User asks: `同步 GitHub 上 MyPlugin 的最新代码到本地`

1. Run `pull` without `--force`.
2. If local conflicts are reported, show the conflicting paths and ask whether
   to force overwrite or resolve manually.

## Final Checklist

- The plugin ID matches the package object key.
- The package file and plugin directory layout match the selected version.
- Sensitive and runtime-local files were rejected or skipped.
- The preview was shown before push, unless explicitly bypassed.
- The final response mentions whether local agent restart is needed only when
  this built-in skill itself changed.
