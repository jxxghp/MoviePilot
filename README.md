# MoviePilot

简体中文 | [English](README_EN.md)

![GitHub Repo stars](https://img.shields.io/github/stars/jxxghp/MoviePilot?style=for-the-badge)
![GitHub forks](https://img.shields.io/github/forks/jxxghp/MoviePilot?style=for-the-badge)
![GitHub contributors](https://img.shields.io/github/contributors/jxxghp/MoviePilot?style=for-the-badge)
![GitHub repo size](https://img.shields.io/github/repo-size/jxxghp/MoviePilot?style=for-the-badge)
![GitHub issues](https://img.shields.io/github/issues/jxxghp/MoviePilot?style=for-the-badge)
![Docker Pulls](https://img.shields.io/docker/pulls/jxxghp/moviepilot?style=for-the-badge)
![Docker Pulls V2](https://img.shields.io/docker/pulls/jxxghp/moviepilot-v2?style=for-the-badge)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20Synology-blue?style=for-the-badge)

基于 [NAStool](https://github.com/NAStool/nas-tools) 部分代码重新设计，聚焦自动化核心需求，减少问题同时更易于扩展和维护。

# 仅用于学习交流使用，请勿在任何国内平台宣传该项目！

发布频道：https://t.me/moviepilot_channel

## 主要特性

- 聚焦影视自动化的核心流程：订阅、搜索、下载、整理、刮削、媒体库刷新与消息通知。
- 前后端分离，后端基于 FastAPI，前端基于 Vue 3，部署和扩展边界更清晰。
- 支持下载器、媒体服务器、元数据源、消息渠道、插件、工作流和 AI Agent 等能力组合。
- 更完整的功能介绍、截图和使用入口见官网：https://movie-pilot.org

## 安装使用

推荐优先使用 Docker 部署，常用镜像包括 `jxxghp/moviepilot-v2` 和 `jxxghp/moviepilot`。Compose 示例、环境变量、目录映射和升级方式以官方 Wiki 为准：

- 官方 Wiki：https://wiki.movie-pilot.org
- PostgreSQL 部署说明：[docs/postgresql-setup.md](docs/postgresql-setup.md)

也可以使用本地 CLI 以源码模式安装和管理 MoviePilot：

```shell
curl -fsSL https://raw.githubusercontent.com/jxxghp/MoviePilot/v2/scripts/bootstrap-local.sh | bash
```

安装完成后使用 `moviepilot` 命令完成初始化、启动、停止、更新和配置查看。完整命令见 [docs/cli.md](docs/cli.md)。

## Agent

1. MoviePilot 自带智能体能力，可在完成模型配置后，通过自然语言调用系统工具，辅助完成搜索、订阅、下载、整理、排障等管理任务。
2. 其它智能体可以导入本仓库的 `skills/` 目录以获得 MoviePilot 操作能力；支持 `skills` CLI 的环境可使用：

   ```shell
   npx skills add https://github.com/jxxghp/MoviePilot
   ```

   内置 Skills 列表见 [skills/](skills/)，自定义 Skill 可参考 [skills/create-moviepilot-skill/SKILL.md](skills/create-moviepilot-skill/SKILL.md)。
3. 其它 MCP 客户端可以通过 MoviePilot 的 MCP 端点 `/api/v1/mcp` 调用工具，认证方式、客户端配置和工具 API 见 [docs/mcp-api.md](docs/mcp-api.md)。


## 参与开发

开发前请先阅读仓库规则和本地环境说明，保持变更聚焦，通过测试后再提交 PR。常用入口：

- 文档规则入口：[docs/rules/README.md](docs/rules/README.md)
- 开发环境与本地源码运行：[docs/development-setup.md](docs/development-setup.md)
- 测试说明：[docs/testing.md](docs/testing.md)
- REST API 文档：https://api.movie-pilot.org
- 插件开发说明：https://wiki.movie-pilot.org/zh/plugindev

## 相关项目

- [MoviePilot-Frontend](https://github.com/jxxghp/MoviePilot-Frontend)
- [MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources)
- [MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)
- [MoviePilot-Server](https://github.com/jxxghp/MoviePilot-Server)
- [MoviePilot-Rust](https://github.com/jxxghp/MoviePilot-Rust)
- [MoviePilot-Wiki](https://github.com/jxxghp/MoviePilot-Wiki)

## 免责申明

- 本软件仅供学习交流使用，任何人不得将本软件用于商业用途，任何人不得将本软件用于违法犯罪活动，软件对用户行为不知情，一切责任由使用者承担。
- 本软件代码开源，基于开源代码进行修改，人为去除相关限制导致软件被分发、传播并造成责任事件的，需由代码修改发布者承担全部责任，不建议对用户认证机制进行规避或修改并公开发布。
- 本项目不接受捐赠，没有在任何地方发布捐赠信息页面，软件本身不收费也不提供任何收费相关服务，请仔细辨别避免误导。

## 贡献者

<a href="https://github.com/jxxghp/MoviePilot/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=jxxghp/MoviePilot" />
</a>
