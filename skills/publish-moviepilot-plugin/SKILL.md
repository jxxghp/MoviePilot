---
name: publish-moviepilot-plugin
version: 6
description: >-
  Use this skill when the user asks to publish, upload, sync, pull, push, diff,
  or maintain a MoviePilot local plugin in a GitHub repository. Covers using the
  configured MoviePilot GitHub token, PLUGIN_LOCAL_REPO_PATHS local plugin
  repositories, package.json/package.v2.json/package.v3.json metadata,
  plugins/plugins.v2/plugins.v3
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

- Publish one local plugin under `plugins.v3/<plugin_id_lower>/`,
  `plugins.v2/<plugin_id_lower>/` or `plugins/<plugin_id_lower>/` to a GitHub
  repository.
- Merge only that plugin's entry into `package.v3.json`, `package.v2.json` or
  `package.json`.
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
  - V3: `package.v3.json` and `plugins.v3/<plugin_id_lower>/`
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
   - Prefer `v3` when the plugin lives under `plugins.v3/<plugin_id_lower>/`
     or the repository ships `package.v3.json` (current MoviePilot V3 hosts).
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

# V3 插件仓库（package.v3.json + plugins.v3/）
python skills/publish-moviepilot-plugin/scripts/publish_plugin.py push \
  --repo owner/repo \
  --plugin-id MyPlugin \
  --local-repo /path/to/MoviePilot-Plugins \
  --package-version v3 \
  --message "Publish MyPlugin v1.0.0"

python skills/publish-moviepilot-plugin/scripts/publish_plugin.py create-repo \
  --repo owner/repo
```

Options:

- `--package-version v3|v2|legacy|auto`: layout selector. `auto` resolves the
  layout from where the plugin directory actually lives
  (`plugins.v3` → `plugins.v2` → `plugins`), then falls back to the manifest
  files, so repositories that still keep both manifests stay correct. Pass
  `v3` explicitly for current MoviePilot V3 plugin repositories.
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

`push` 通过 Git Data API（blob → tree → commit → 移动分支引用）提交：无论一次改动
多少文件都只产生**一个 commit**，CI 只会触发一次、diff 完整，`package` 版本与源码
版本不会出现中间态；返回 JSON 中带 `commit_sha`。空仓库会自动创建根提交并建立分支。

## GitHub Release 与 README

`push` 只同步插件源码与 package 条目，**不会创建 GitHub Release，也不会修改
README**。目标仓库若用 GitHub Actions 打包（例如
`.github/workflows/plugins-release.yml`，产物为 `Plugin_vX.Y.Z` tag 与
zip/tar.gz + sha256 资产）：

- package 条目必须显式写 `"release": true`，否则工作流即使运行也不会发版。
- 自动触发依赖**同一次 push 中 package 文件的 version 发生变化**。逐文件提交时
  `package.v3.json` 的变更可能落在另一个提交，diff 里看不到它，于是静默跳过；
  这种情况改用 `workflow_dispatch`（`source_directory` + `plugin_id`）手动触发。
- README 的插件表格与说明需要单独编辑并推送（Contents API 或 git），不在
  `push` 的职责范围内。

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
- For pull operations, preserve excluded local files even with `--force`.
  Invalid local package JSON stops the pull before any local file is written,
  including in `--force` mode.
  Differing local plugin files or package metadata stop the entire pull unless
  `--force` is used. Package entries for other plugins remain untouched.
- With the `auto` layout, inspect each configured local repository separately;
  the working directory does not determine the layout of other repositories.

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
