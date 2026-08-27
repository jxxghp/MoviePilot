# 02 — Tech Stack

## Runtime and Language

| Item | Detail |
|---|---|
| Language | Python 3.14+ |
| Primary CI Python version | Python 3.14 |
| Dependency compatibility CI | Python 3.14 supported-platform matrix plus Linux amd64/arm64 standard and free-threaded Docker profiles |
| Async runtime | asyncio (native), integrated with FastAPI/Uvicorn |

---

## Backend Framework

| Item | Detail |
|---|---|
| Web framework | FastAPI |
| ASGI server | Uvicorn |
| Data validation | Pydantic v2 (`BaseModel`, `BaseSettings`, `model_validator`) |
| Settings management | `pydantic-settings` (`BaseSettings` class in `app/runtime/config.py`) |

---

## Database

| Item | Detail |
|---|---|
| Default database | SQLite |
| Optional database | PostgreSQL (configured via `DB_TYPE` and related env vars) |
| ORM | SQLAlchemy |
| Migration tool | Alembic (`database/versions/`) |
| PostgreSQL extras | `app/modules/postgresql/` module; setup guide at `docs/postgresql-setup.md` |

---

## Caching

| Item | Detail |
|---|---|
| File-based cache | `FileCache` / `AsyncFileCache` in `app/runtime/cache.py` |
| Redis | Optional; `app/modules/redis/` module; used for distributed caching when configured |
| In-process cache | Decorator helpers `fresh` / `async_fresh` on `FileCache` |

---

## LLM and AI Agent

| Item | Detail |
|---|---|
| Agent runtime | `app/agent/` — custom LLM agent orchestration |
| LLM abstraction | LangChain-based with multi-provider support |
| Supported providers | OpenAI-compatible APIs, Anthropic, and other configurable providers |
| Configuration | `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL` in settings |
| Enable flag | `AI_AGENT_ENABLE` |
| MCP protocol | JSON-RPC 2.0 at `/api/v1/mcp`; see `docs/mcp-api.md` |

---

## Module Integrations

### Download Clients
| Module | Directory |
|---|---|
| qBittorrent | `app/modules/qbittorrent/` |
| Transmission | `app/modules/transmission/` |
| rTorrent | `app/modules/rtorrent/` |

### Media Servers
| Module | Directory |
|---|---|
| Emby | `app/modules/emby/` |
| Jellyfin | `app/modules/jellyfin/` |
| Plex | `app/modules/plex/` |
| TrimMedia | `app/modules/trimemedia/` |
| Zspace | `app/modules/zspace/` |
| Ugreen | `app/modules/ugreen/` |

### Message Channels
| Module | Directory |
|---|---|
| Telegram | `app/modules/telegram/` |
| WeChat | `app/modules/wechat/` |
| WeChatClawBot | `app/modules/wechatclawbot/` |
| Feishu | `app/modules/feishu/` |
| Slack | `app/modules/slack/` |
| Discord | `app/modules/discord/` |
| VoceChat | `app/modules/vocechat/` |
| Synology Chat | `app/modules/synologychat/` |
| WebPush | `app/modules/webpush/` |
| QQBot | `app/modules/qqbot/` |

### Metadata Sources
| Module | Directory |
|---|---|
| TMDB | `app/modules/themoviedb/` |
| TheTVDB | `app/modules/thetvdb/` |
| Douban | `app/modules/douban/` |
| Bangumi | `app/modules/bangumi/` |
| Fanart | `app/modules/fanart/` |

---

## Dependency Management

| Item | Detail |
|---|---|
| Project metadata | `pyproject.toml` — runtime dependencies in `[project].dependencies`, development tooling in `[dependency-groups].dev` |
| Lock | `uv.lock` — committed resolution for Python 3.14+ and supported platforms |
| Package manager | uv 0.12.5+（推荐最新稳定版） |
| Runtime install | `uv sync --locked --no-dev --no-install-project` |
| Dev/test/lint/build install | `uv sync --locked` |
| Supported platforms | Linux x86_64/arm64, macOS x86_64/arm64, Windows x64 |

---

## Performance Extension

| Item | Detail |
|---|---|
| Rust extension | `moviepilot_rust` — optional compiled accelerator for core processing paths |
| Install | Installed from the `moviepilot-rust` PyPI package with normal Python dependencies |
| Source | Maintained in the separate `MoviePilot-Rust` repository |
| Toggle | Can be disabled/re-enabled at runtime via frontend Advanced Settings → Lab |

---

## Quality Tooling

| Tool | Purpose | Command |
|---|---|---|
| pytest | Test runner | `uv run --locked --no-sync pytest tests/test_xxx.py` |
| pylint | Static analysis | `uv run --locked --no-sync pylint app/` |
| uv | Lock and environment consistency | `uv lock --check && uv sync --locked --offline --inexact --no-dev --check` |
| pip-audit | Locked dependency vulnerability scan | `uv export --quiet --locked --no-dev --no-emit-project -o /tmp/moviepilot-audit-requirements.txt && uvx --from pip-audit pip-audit --require-hashes --disable-pip --strict --progress-spinner off -r /tmp/moviepilot-audit-requirements.txt` |

---

## Deployment

| Method | Detail |
|---|---|
| Docker | Primary deployment; image bundles backend + frontend static files + resources |
| Local CLI | `moviepilot` CLI for source-based install; see `docs/cli.md` |
| Frontend | Vue/TypeScript SPA served from `public/`; source in `MoviePilot-Frontend` repo |
| Frontend proxy | Local Node `service.js` proxies `/api` and `/cookiecloud` to the backend |

*Last Updated: 2026-08-19*
