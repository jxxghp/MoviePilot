---
name: create-moviepilot-plugin
version: 3
description: >-
  Use this skill when the user asks to create, modify, debug, validate, or
  scaffold a MoviePilot local plugin. Covers MoviePilot V2 plugin development,
  _PluginBase implementations, package.v2.json/package.json market metadata,
  plugins.v2/plugins source layout, PLUGIN_LOCAL_REPO_PATHS local plugin
  sources, plugin APIs, Vuetify JSON forms/pages/dashboards, Vue module
  federation remote components, get_render_mode, get_sidebar_nav, plugin
  sidebar pages, commands, services, workflow actions, agent tools, and local
  install/reload flows. Also use for Chinese requests mentioning 编写插件、本地插件源,
  插件开发, V2插件, 插件市场, 本地安装插件, 插件热加载, 前端联邦, 侧栏入口, Vue插件页面.
allowed-tools: list_directory read_file write_file edit_file execute_command search_web browse_webpage query_system_settings update_system_settings query_market_plugins install_plugin reload_plugin query_installed_plugins
---

# Create MoviePilot Plugin

Use this skill to build or revise MoviePilot plugins that can be developed from
a local plugin source and installed into the running MoviePilot instance.

## Ground Truth

- Host plugin contract: `app/plugins/__init__.py`, especially `_PluginBase`.
- Host plugin discovery, local source sync, install, reload: `app/core/plugin.py`
  and `app/helper/plugin.py`.
- Host plugin endpoints, API auth, static files, remotes, and sidebar nav:
  `app/api/endpoints/plugin.py`.
- Local development note: `docs/development-setup.md`.
- Plugin repository conventions: `MoviePilot-Plugins` uses `plugins.v2/` with
  `package.v2.json` for V2 plugins; legacy or cross-generation entries may use
  `plugins/` with `package.json`.
- When working in or from `MoviePilot-Plugins`, read its `README.md`,
  `docs/Repository_Guide.md`, and `docs/V2_Plugin_Development.md`. For
  scenario-specific extensions, read the matching `docs/faq/*.md`.

## Code Tool Workflow

- Use `execute_command(action="run")` with `rg` and narrow globs or paths to
  locate plugin classes, extension points, tests, and package entries. Use
  `list_directory` only when inspecting one known folder or a configured remote
  storage backend.
- Read the relevant implementation and adjacent example before editing.
- Before using a Python or Node.js dependency API, determine the exact installed
  or locked version from requirements, package manifests, lockfiles, local
  package source, and `.pyi`/`.d.ts` declarations. If those are insufficient,
  use `search_web` with the official documentation domain and `browse_webpage`
  to read the matching version. Do not guess API signatures from memory or mix
  examples from different major versions. Search the relevant package directory,
  `.venv`, or `node_modules` directly with `rg` instead of scanning the entire
  project without bounds.
- Use `edit_file` for localized changes. Its `old_text` must identify one exact
  location by default; add surrounding context instead of enabling
  `replace_all` unless every match intentionally changes.
- Use `write_file` for new files. Existing files require `overwrite=true` for a
  full rewrite; first call `read_file(include_metadata=true)` and pass its
  `sha256` as `expected_sha256` when replacing previously read content.
- Use `execute_command(action="run")` for short validation, Git, and diagnostic
  commands. Use `action="start"` only for interactive or long-running commands,
  then continue through the returned session ID.
- Do not use shell redirection or inline scripts to perform source edits or to
  bypass a file-tool permission error.
- When the plugin uses Vue federation, also read
  `MoviePilot-Frontend/docs/module-federation-guide.md`,
  `MoviePilot-Frontend/docs/federation-troubleshooting.md`,
  `MoviePilot-Frontend/src/utils/federationLoader.ts`, and
  `MoviePilot-Frontend/src/pages/plugin-app.vue`.
- Repository boundaries: `MoviePilot` owns runtime loading, API registration,
  events, services, data, and permissions; `MoviePilot-Frontend` owns plugin UI
  rendering, federation loading, and sidebar pages; `MoviePilot-Plugins` owns
  plugin source, icons, package indexes, and release metadata.

## Pre-Flight

1. Understand the user request: plugin purpose, trigger mode, configuration,
   output UI, whether it needs a scheduler, API, command, workflow action, or
   agent tool.
2. Run the UI Mode Selection Gate before writing any UI code.
   - If the user already explicitly chose JSON config/Vuetify JSON or Vue
     federation, follow that choice.
   - If the plugin has any UI surface and the user has not chosen a mode, ask
     them to choose between the two modes below and wait for the answer before
     implementing UI files or schemas.
   - Do not silently default to either mode just because one seems easier.
3. Inspect existing plugins before creating a new one:
   - Local runtime examples: `app/plugins/<plugin>/__init__.py`
   - Market/local source candidates: use `query_market_plugins` when the
     running instance is available.
   - Installed plugin candidates: use `query_installed_plugins`; its summaries
     include `repo_url` when the source can be matched from a local plugin
     repository or plugin market metadata.
   - For Vue federation examples, prefer current compliant plugins such as
     `MoviePilot-Plugins/plugins.v2/agenttokens/` and the frontend example
     `MoviePilot-Frontend/examples/plugin-component/`.
4. Determine the target source path:
   - Query `PLUGIN_LOCAL_REPO_PATHS` with `query_system_settings` when possible.
   - If exactly one local plugin repository is configured, prefer that path.
   - If several are configured, choose the one the user named; otherwise ask
     which repository to use.
   - If none is configured, set it before writing plugin code:
     `update_system_settings(setting_key="PLUGIN_LOCAL_REPO_PATHS", value="local-plugins", operation="replace")`.
     `local-plugins` is resolved relative to the MoviePilot root by the local
     plugin source loader. Create that source directory and write the plugin
     under it; do not write new plugin source directly into `app/plugins/`
     unless the user explicitly asks for a runtime-only experiment.
5. Choose the plugin ID:
   - Class name is the plugin ID, for example `MyNotifier`.
   - Directory name is the class name lowercased, for example `mynotifier`.
   - Avoid collisions with installed or market plugins unless the user is
     explicitly modifying that plugin.
   - Do not hardcode the original plugin ID for data/config namespaces when the
     plugin may support clones; use `self.__class__.__name__`.

## UI Mode Selection Gate

MoviePilot plugin UI has exactly two implementation modes. Make the user choose
one whenever the request includes configuration, detail pages, dashboards,
sidebar pages, or any other plugin UI and the mode is not already explicit.

Ask a concise question like:

```text
这个插件 UI 用哪种方式实现？
1. JSON 配置：后端返回 Vuetify JSON，适合普通配置表单、简单详情页和轻量仪表板。
2. 联邦 UI：独立 Vue 远程组件，适合复杂交互、自定义布局、侧栏全页或多页面。
```

Selection rules:

- **JSON config / Vuetify JSON**: implement `get_form()`, `get_page()`, and
  `get_dashboard()` with JSON component schemas. No frontend build or
  `dist/assets/remoteEntry.js` is needed.
- **Federation UI / Vue remote component**: implement `get_render_mode()`,
  expose Vue components through Vite federation, build frontend assets into the
  plugin directory, and use `get_sidebar_nav()` only when a sidebar page is
  requested.
- If the plugin truly has no user-facing UI, state that no UI mode is needed
  and implement only the backend extension points the request requires.
- Backend-only work may proceed while waiting only if it cannot constrain or
  preclude either UI mode.

## Local Source Layout

Default to V2 layout for new local plugins:

```text
<local-plugin-repo>/
├── package.v2.json
└── plugins.v2/
    └── <plugin_id_lower>/
        ├── __init__.py
        ├── requirements.txt        # only when extra runtime dependencies are necessary
        └── ...                     # helper modules, schemas, static assets
```

For a Vue federation plugin, the runtime requirement is the built remote assets
under the plugin directory:

```text
plugins.v2/<plugin_id_lower>/
├── __init__.py
├── dist/
│   └── assets/
│       ├── remoteEntry.js
│       └── ...                     # JS/CSS/assets referenced by remoteEntry
├── package.json                    # optional frontend build project metadata
├── vite.config.js                  # optional frontend build config
└── src/                            # optional source, not required at runtime
```

Do not rely on frontend source files at runtime. If the source is kept in the
plugin repository for maintainability, still build and ship the `dist/assets`
files required by `remoteEntry.js`.

Only use the legacy layout when the user explicitly needs it:

```text
<local-plugin-repo>/
├── package.json
└── plugins/
    └── <plugin_id_lower>/
        └── __init__.py
```

For legacy `package.json` entries that should work on V2, include `"v2": true`.
For V2-first work, prefer `package.v2.json` and `plugins.v2/`.

## Package Metadata

Add or update the package entry for the plugin ID. Keep the package version and
the class `plugin_version` synchronized.

```json
{
  "MyNotifier": {
    "name": "通知示例",
    "description": "根据用户配置发送示例通知。",
    "labels": "消息通知",
    "version": "1.0.0",
    "icon": "mynotifier.png",
    "author": "local",
    "level": 1,
    "system_version": ">=2.12.0",
    "history": {
      "v1.0.0": "初始版本"
    }
  }
}
```

Rules:

- The package object key must match the plugin class name.
- `version` must match `plugin_version`.
- `name`, `description`, `icon`, `author`, `labels`, and `level` should match
  the plugin class attributes when those attributes exist (`plugin_name`,
  `plugin_desc`, `plugin_icon`, `plugin_author`, `plugin_label`, `auth_level`).
- `history` should record user-readable changes for each published version.
- Use `system_version` when the plugin depends on a host capability introduced
  in a specific MoviePilot version, including new backend APIs, helpers, events,
  Vue federation behavior, sidebar nav, dashboard behavior, or agent tools.
- Use `"release": true` only when the plugin is intentionally distributed by a
  GitHub Release archive.
- New plugin entries should usually be appended to the package index so they
  appear as newer marketplace items.
- Do not add dependencies unless they are actually required. If
  `requirements.txt` changes, the user must reinstall the plugin; hot reload is
  not enough to install dependencies.
- Plugin dependencies are installed into the shared MoviePilot Python
  environment. Do not pin or downgrade packages already provided by MoviePilot
  unless the user has explicitly accepted the compatibility risk.

## Implementation Skeleton

Implement all abstract methods from `_PluginBase`. All new functions and
methods need Chinese docstrings; public classes, public methods, and public
functions are a hard review gate.

```python
from typing import Any, Dict, List, Optional, Tuple

from app.plugins import _PluginBase


class MyNotifier(_PluginBase):
    """通知示例插件。"""

    plugin_name = "通知示例"
    plugin_desc = "根据用户配置发送示例通知。"
    plugin_icon = "mynotifier.png"
    plugin_version = "1.0.0"
    plugin_label = "消息通知"
    plugin_author = "local"
    plugin_config_prefix = "mynotifier_"
    plugin_order = 100
    auth_level = 1

    _enabled = False
    _message = ""

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._enabled = False
        self._message = ""
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        self._message = str(config.get("message") or "")

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return []

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "enabled",
                            "label": "启用插件"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "message",
                            "label": "通知内容"
                        }
                    }
                ]
            }
        ], {
            "enabled": False,
            "message": ""
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。"""
        if not self._enabled:
            return None
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "text": self._message or "插件已启用"
                }
            }
        ]

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        return None
```

## Extension Points

Use only the extension points the requested plugin actually needs:

- Configuration: `get_form()` returns Vuetify form schema and default data;
  `init_plugin()` reads config; `update_config()` persists internal changes.
- Data: use `save_data()`, `get_data()`, `del_data()`, and `get_data_path()`.
- Notification: use `post_message()` instead of directly calling message
  modules.
- APIs: return route definitions from `get_api()`; default auth is `apikey`
  when `auth` is omitted. Vue component APIs should normally use
  `auth: "bear"` and be called through the `api` prop passed by the frontend.
- Commands: return slash-command definitions from `get_command()` and dispatch
  through MoviePilot events.
- Services: return scheduler services from `get_service()` and always clean
  them up in `stop_service()`.
- Dashboards: use `get_dashboard_meta()` and `get_dashboard()` for homepage
  widgets.
- Workflow actions: use `get_actions()`; action functions receive
  `ActionContent` first and return `(success, action_content)`.
- Agent tools: use `get_agent_tools()`; each tool class must inherit
  `app.agent.tools.base.MoviePilotTool`.
- Custom Vue UI: implement `get_render_mode()` only when Vuetify schema cannot
  satisfy the request. Return `("vue", "<compiled-assets-path>")` and include
  built frontend assets in the plugin directory.

## Vue Federation UI

Use Vue federation only after the Pre-Flight UI decision says JSON schema is not
enough. A Vue plugin must align backend methods, built files, and federation
exposes.

Backend requirements:

```python
from typing import Any, Dict, List, Tuple


@staticmethod
def get_render_mode() -> Tuple[str, str]:
    """声明插件使用 Vue 联邦组件渲染。"""
    return "vue", "dist/assets"


def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
    """Vue 模式下返回默认配置模型。"""
    return [], self._current_config()


def get_page(self) -> List[dict]:
    """Vue 模式下详情页由远程 Page 组件渲染。"""
    return []
```

When the plugin needs a main-layout sidebar page, also implement:

```python
def get_sidebar_nav(self) -> List[Dict[str, Any]]:
    """声明插件在主界面左侧导航栏中的全页入口。"""
    if not self.get_state():
        return []
    return [
        {
            "nav_key": "main",
            "title": "我的插件",
            "icon": "mdi-puzzle",
            "section": "system",
            "permission": "manage",
            "order": 10,
        }
    ]
```

Sidebar rules:

- Sidebar entries are only aggregated for enabled plugins whose
  `get_render_mode()` returns `"vue"`.
- `section` must be one of `start`, `discovery`, `subscribe`, `organize`,
  `system`; invalid values fall back to `system`.
- `permission` may be `subscribe`, `discovery`, `search`, `manage`, or `admin`;
  invalid values are ignored.
- `nav_key` defaults to `main` and must not contain `/`, `?`, `#`, or spaces.
- Multiple sidebar entries are allowed; each entry needs a stable `nav_key`.

Frontend federation requirements:

```js
federation({
  name: 'MyPlugin',
  filename: 'remoteEntry.js',
  exposes: {
    './Page': './src/components/Page.vue',
    './Config': './src/components/Config.vue',
    './Dashboard': './src/components/Dashboard.vue',
    './AppPage': './src/components/AppPage.vue',
    './AppPageSettings': './src/components/AppPageSettings.vue',
  },
  shared: {
    vue: { requiredVersion: false, generate: false },
    vuetify: { requiredVersion: false, generate: false, singleton: true },
    'vuetify/styles': { requiredVersion: false, generate: false, singleton: true },
  },
  format: 'esm',
})
```

Build requirements:

- Set Vite `build.target` to `esnext` because federation uses top-level await.
- Use `cssCodeSplit: true` and scoped/component-local styles where possible.
- Build with the frontend project's documented command, then keep `remoteEntry.js`
  and every JS/CSS/asset file it references under `dist/assets`.
- Do not add frontend runtime dependencies to the plugin Python
  `requirements.txt`; keep frontend dependencies in the frontend build project.

Component contracts:

- `Page` renders the plugin detail dialog and may emit `action`, `switch`, and
  `close`.
- `Config` renders plugin settings, receives `initialConfig` and `api`, and
  emits `save`, `close`, and `switch`.
- `Dashboard` receives `config` and `allowRefresh`.
- `AppPage` renders the main-layout sidebar page and receives `api`, `pluginId`,
  and `navKey`.
- For sidebar `nav_key=main`, the frontend loads `./AppPage` then `./Page`.
- For any other `nav_key`, the frontend loads `./AppPage{PascalCase(nav_key)}`,
  then `./AppPage`, then `./Page`. Examples: `settings -> AppPageSettings`,
  `my_tool -> AppPageMyTool`.
- A single `AppPage` may branch on `navKey`, or separate
  `AppPage{PascalCase}` files may be exposed for specific entries.

Vue API calls:

- Define frontend-facing plugin APIs with `auth: "bear"`.
- Call them with the injected API object, for example
  `props.api.get(\`plugin/${props.pluginId}/history\`)`.
- Do not pass `settings.API_TOKEN` into Vue components for browser-side calls.

## Local Install And Reload

1. After writing files in a configured local plugin repository, call
   `query_market_plugins(query="<PluginID>", force_refresh=True)` to confirm the
   local source is visible.
2. Install or reinstall with `install_plugin(plugin_id="<PluginID>", force=True)`.
   The install flow copies the source into `app/plugins/<plugin_id_lower>/`.
3. If `PLUGIN_AUTO_RELOAD` or development mode is enabled, Python source changes
   in an installed local plugin can auto-sync and reload. If it is not enabled,
   call `reload_plugin(plugin_id="<PluginID>")` after editing runtime files.
4. When `requirements.txt` changes, reinstall with `force=True`; reloading alone
   does not install new dependencies.

## Validation

- Re-read the changed files and confirm class name, directory name, package ID,
  and package version are consistent.
- Confirm every public class, public method, and public function has a Chinese
  docstring.
- Confirm every newly written function or method has a Chinese docstring, even
  when it is private helper code.
- For Vue federation plugins, confirm `get_render_mode()` returns
  `("vue", "dist/assets")` or the actual built asset path, and that
  `dist/assets/remoteEntry.js` exists.
- For sidebar plugins, confirm the plugin is enabled, `get_state()` returns
  `True`, `get_sidebar_nav()` returns valid items, and matching `AppPage`
  exposes exist for all non-main `nav_key` values or a generic `AppPage` handles
  them.
- Confirm frontend-facing API routes use `auth: "bear"` and browser code calls
  them through the provided `api` prop.
- Keep external HTTP calls behind MoviePilot utilities and avoid real network
  calls in tests.
- If the plugin has non-trivial logic, add or update pytest-native tests. Plugin
  repositories can use `app.testing.bootstrap.prepare_v2_backend()` to prepare a
  temporary MoviePilot backend and inject `<repo>/plugins.v2` into `sys.path`.
- Run the narrowest allowed validation for the touched area. In this repository,
  follow `docs/rules/03-commands.md`; for plugin-only repositories, follow their
  own documented validation commands.
- For plugin repository Python changes, use the host Python environment when
  possible and run at least syntax compilation for touched plugin files.
- For Vue federation changes, run the frontend project's documented typecheck
  and build commands when available, then verify the built assets were copied to
  the plugin directory.

## Vue Federation Troubleshooting

- `GET /api/v1/plugin/remotes?token=moviepilot` should include the plugin with a
  URL ending in `/plugin/file/<plugin_id_lower>/<dist_path>/remoteEntry.js`.
- `GET /api/v1/plugin/sidebar_nav` should include sidebar entries for enabled
  Vue plugins with valid `nav_key`, `section`, and `permission`.
- If the console says `Module name 'vue' does not resolve to a valid URL`, check
  the federation `shared` config and use `requiredVersion: false`.
- If the console says top-level await is unavailable, set `build.target` to
  `esnext`.
- If dynamic import fails, check the remote file request status, the computed
  `remoteEntry.js` path, and whether the installed runtime plugin directory
  actually contains the built assets.
- If a sidebar page is blank, check the expose name resolution for the current
  `nav_key` and fallbacks (`AppPage{PascalCase}` -> `AppPage` -> `Page`).

## Final Report

Report:

- Plugin ID, source path, and runtime path if installed.
- Package file changed (`package.v2.json` or `package.json`).
- UI mode used (`vuetify` JSON or `vue` federation), and for Vue plugins the
  exposed components and built asset path.
- Whether the plugin was installed or reloaded.
- Validation commands run, or why validation was not run.
