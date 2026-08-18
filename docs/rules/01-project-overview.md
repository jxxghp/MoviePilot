# 01 — Project Overview

## System Purpose

MoviePilot is a self-hosted media automation platform targeting Chinese-language users. It automates the full lifecycle of media acquisition and organization:

1. **Discovery** — monitors RSS feeds, subscription lists, and recommendation sources for new media releases.
2. **Search** — queries configured torrent indexers to locate suitable torrents for subscribed media.
3. **Download** — sends torrent tasks to a configured download client (qBittorrent, Transmission, rTorrent).
4. **Transfer** — moves or hard-links completed downloads into a structured media library.
5. **Scraping** — fetches metadata (posters, descriptions, episode info) from TMDB, TheTVDB, Douban, and Bangumi.
6. **Media Server Integration** — notifies and refreshes Emby, Jellyfin, or Plex after files are organized.
7. **Messaging** — sends status notifications through Telegram, WeChat, Feishu, Slack, Discord, and other channels.
8. **AI Agent** — provides a conversational agent interface (via MCP and LLM chain) for natural-language management tasks.

---

## Repository Boundaries

### What Is in This Repository

| Path | Content |
|---|---|
| `app/` | FastAPI backend application |
| `moviepilot` | Local CLI entrypoint (install, init, start, stop, update, agent) |
| `app/api/endpoints/` | HTTP endpoint handlers |
| `app/application/orchestration/` | Business orchestration layer (use-case chains) |
| `app/modules/` | Pluggable backend integrations (downloaders, media servers, etc.) |
| `app/db/` | SQLAlchemy models and data access wrappers |
| `app/foundation/` | Stateless general-purpose primitives |
| `app/domain/` | Media-domain models, parsing, and rules |
| `app/runtime/` | Config, events, logging, caching, concurrency, process state, extensions, and legacy compatibility |
| `app/adapters/` | Cache, network, system, generated-resource, and named external-product adapters |
| `app/runtime/extensions/` | Module, plugin, and configured-service lifecycle management |
| `app/application/messaging/` | Messaging, interaction, and Agent-to-message capabilities (`interaction.py` contracts, `router.py` priority/callback dispatch, `site.py`/`subscribe.py`/`skill.py` command sessions, `media.py` media interaction state, `plugin.py` plugin input, `agent.py` agent choice bridge, `message.py` rendering and queue); not a public plugin SDK |
| `app/application/security/` | Authentication and access-control capabilities |
| `app/application/` | Focused application services |
| `app/sdk/` | Stable imports for plugins |
| `app/runtime/compat/` | Virtual legacy import compatibility and DEBUG diagnostics |
| `app/schemas/` | Pydantic request/response models and shared enums |
| `app/agent/` | LLM Agent runtime, tools, middleware, and Skill lifecycle |
| `app/workflow/` | Workflow engine |
| `database/versions/` | Alembic migration scripts |
| `docs/` | CLI, MCP/API, and development workflow documentation |
| `skills/` | AI agent skills and associated scripts |
| `tests/` | Pytest test suite |

### What Is NOT in This Repository

* **Frontend source code** — lives in the separate `MoviePilot-Frontend` repository (Vue/TypeScript). Only the built `dist/` artifact is consumed here.
* **Plugin source code** — plugins are installed into `app/plugins/` at runtime from external sources; they are not part of this repository.
* **User config and runtime data** — `config/`, `.moviepilot.env`, `*.db` files are local runtime state. Do not modify or commit them unless explicitly requested.

---

## Deployment Models

### Docker (Primary)

The standard deployment method. A Docker image bundles the backend, frontend static files, and resource data. Users configure via environment variables and mount a config directory.

### Local CLI

An alternative for users running from source. The `moviepilot` CLI handles installation, initialization, service management, and updates. See `docs/cli.md` for the full command reference.

---

## Key External Dependencies (Domain Context)

| Service Type | Supported Backends |
|---|---|
| Torrent indexers | Site-specific spiders, Jackett/Prowlarr compatible |
| Download clients | qBittorrent, Transmission, rTorrent |
| Media servers | Emby, Jellyfin, Plex, TrimMedia, Zspace, Ugreen |
| Metadata sources | TMDB, TheTVDB, Douban, Bangumi, Fanart |
| Message channels | Telegram, WeChat, WeChatClawBot, Feishu, Slack, Discord, VoceChat, Synology Chat, WebPush, QQBot |
| LLM providers | OpenAI-compatible, Anthropic, and other configurable providers |

---

## Business Domain Vocabulary

| Term | Meaning |
|---|---|
| Subscribe | A tracked media item (movie or TV series) that MoviePilot will automatically search and download |
| Transfer | The process of moving or hard-linking downloaded files into the organized media library |
| Chain | A business orchestration class that coordinates multiple modules for a use case |
| Module | A pluggable backend integration loaded by the module manager |
| Skill | A packaged AI agent capability that can be invoked via the MCP interface |
| SystemConfig | Runtime key-value configuration stored in the database and managed via `SystemConfigKey` |

*Last Updated: 2026-08-14*
