# MoviePilot

[简体中文](README.md) | English

![GitHub Repo stars](https://img.shields.io/github/stars/jxxghp/MoviePilot?style=for-the-badge)
![GitHub forks](https://img.shields.io/github/forks/jxxghp/MoviePilot?style=for-the-badge)
![GitHub contributors](https://img.shields.io/github/contributors/jxxghp/MoviePilot?style=for-the-badge)
![GitHub repo size](https://img.shields.io/github/repo-size/jxxghp/MoviePilot?style=for-the-badge)
![GitHub issues](https://img.shields.io/github/issues/jxxghp/MoviePilot?style=for-the-badge)
![Docker Pulls](https://img.shields.io/docker/pulls/jxxghp/moviepilot?style=for-the-badge)
![Docker Pulls V2](https://img.shields.io/docker/pulls/jxxghp/moviepilot-v2?style=for-the-badge)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20Synology-blue?style=for-the-badge)

Redesigned from parts of [NAStool](https://github.com/NAStool/nas-tools), with a stronger focus on core automation scenarios while reducing issues and making the project easier to extend and maintain.

# For learning and personal communication only. Please do not promote this project on platforms in mainland China.

Release channel: https://t.me/moviepilot_channel

## Key Features

- Focuses on the core media automation flow: subscriptions, search, downloads, file organization, scraping, media server refresh, and notifications.
- Uses a separated backend/frontend architecture: FastAPI for the backend and Vue 3 for the frontend.
- Connects download clients, media servers, metadata providers, message channels, plugins, workflows, and AI Agent capabilities.
- For feature details, screenshots, and product entry points, see https://movie-pilot.org

## Installation and Usage

Docker is the recommended deployment model. Common images include `jxxghp/moviepilot-v2` and `jxxghp/moviepilot`. Compose examples, environment variables, volume mappings, and upgrade notes are maintained in the official wiki:

- Official wiki: https://wiki.movie-pilot.org
- PostgreSQL setup: [docs/postgresql-setup.md](docs/postgresql-setup.md)

MoviePilot can also be installed and managed from source with the local CLI:

```shell
curl -fsSL https://raw.githubusercontent.com/jxxghp/MoviePilot/v2/scripts/bootstrap-local.sh | bash
```

After installation, use the `moviepilot` command for initialization, service management, updates, and configuration. See [docs/cli.md](docs/cli.md) for the full command reference.

## Agent

1. MoviePilot includes a built-in AI Agent. After model configuration, it can call system tools through natural language to help with search, subscriptions, downloads, organization, diagnostics, and other management tasks.
2. Other agents can import the repository `skills/` directory to gain MoviePilot operation capabilities. Environments that support the `skills` CLI can use:

   ```shell
   npx skills add https://github.com/jxxghp/MoviePilot
   ```

   Built-in skills live in [skills/](skills/). For custom skill authoring, see [skills/create-moviepilot-skill/SKILL.md](skills/create-moviepilot-skill/SKILL.md).
3. Other MCP clients can call MoviePilot tools through `/api/v1/mcp`. Authentication, client configuration, and tool APIs are documented in [docs/mcp-api.md](docs/mcp-api.md).

## Development

Before contributing, read the repository rules and local environment guide, keep changes focused, and validate them before opening a PR. Useful entry points:

- Rule index: [docs/rules/README.md](docs/rules/README.md)
- Development setup and local source run: [docs/development-setup.md](docs/development-setup.md)
- Testing guide: [docs/testing.md](docs/testing.md)
- REST API documentation: https://api.movie-pilot.org
- Plugin development guide: https://wiki.movie-pilot.org/zh/plugindev

## Related Projects

- [MoviePilot-Frontend](https://github.com/jxxghp/MoviePilot-Frontend)
- [MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources)
- [MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)
- [MoviePilot-Server](https://github.com/jxxghp/MoviePilot-Server)
- [MoviePilot-Rust](https://github.com/jxxghp/MoviePilot-Rust)
- [MoviePilot-Wiki](https://github.com/jxxghp/MoviePilot-Wiki)

## Disclaimer

- This software is for learning and personal communication only. It must not be used for commercial purposes or illegal activities. The software does not know how users choose to use it, and all responsibility rests with the user.
- The source code is open source and derived from other open-source code. If someone removes the relevant restrictions and redistributes or publishes modified versions that lead to liability events, the publisher of those modifications bears full responsibility. Public releases that bypass or alter the user authentication mechanism are not recommended.
- This project does not accept donations and has not published any donation page anywhere. The software itself is free of charge and does not provide paid services. Please verify information carefully to avoid being misled.

## Contributors

<a href="https://github.com/jxxghp/MoviePilot/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=jxxghp/MoviePilot" />
</a>
