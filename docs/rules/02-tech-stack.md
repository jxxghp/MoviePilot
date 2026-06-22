# 02 — Tech Stack

## Runtime and Language

| Item | Detail |
|---|---|
| Language | Python 3.11+ |
| CI Python version | Python 3.12 |
| Async runtime | asyncio (native), integrated with FastAPI/Uvicorn |

---

## Backend Framework

| Item | Detail |
|---|---|
| Web framework | FastAPI |
| ASGI server | Uvicorn |
| Data validation | Pydantic v2 (`BaseModel`, `BaseSettings`, `model_validator`) |
| Settings management | `pydantic-settings` (`BaseSettings` class in `app/core/config.py`) |

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
| File-based cache | `FileCache` / `AsyncFileCache` in `app/core/cache.py` |
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
| Runtime source | `requirements.in` — production/runtime dependencies only |
| Dev/test/lint/build source | `requirements-dev.in` — includes runtime plus pytest, coverage tooling, pylint, and build support |
| Compatibility entry | `requirements.txt` — delegates to `requirements.in`; not a committed cross-platform lock |
| Runtime install | `pip install -r requirements.txt` |
| Dev/test/lint/build install | `pip install -r requirements-dev.in` |

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
| pytest | Test runner | `pytest tests/test_xxx.py` |
| pylint | Static analysis | `pylint app/` |
| safety | Dependency vulnerability scan | `safety check -r requirements.txt --policy-file=safety.policy.yml` |

---

## Deployment

| Method | Detail |
|---|---|
| Docker | Primary deployment; image bundles backend + frontend static files + resources |
| Local CLI | `moviepilot` CLI for source-based install; see `docs/cli.md` |
| Frontend | Vue/TypeScript SPA served from `public/`; source in `MoviePilot-Frontend` repo |
| Frontend proxy | Local Node `service.js` proxies `/api` and `/cookiecloud` to the backend |

*Last Updated: 2026-05-25*
