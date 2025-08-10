# MoviePilot

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

- 前后端分离，基于FastApi + Vue3。
- 聚焦核心需求，简化功能和设置，部分设置项可直接使用默认值。
- 重新设计了用户界面，更加美观易用。

## 安装使用

官方Wiki：https://wiki.movie-pilot.org

## 参与开发

API文档：https://api.movie-pilot.org

本地运行需要 `Python 3.12`、`Node JS v20.12.1`

- 克隆主项目 [MoviePilot](https://github.com/jxxghp/MoviePilot) 
```shell
git clone https://github.com/jxxghp/MoviePilot
```
- 克隆资源项目 [MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources) ，将 `resources` 目录下对应平台及版本的库 `.so`/`.pyd`/`.bin` 文件复制到 `app/helper` 目录
```shell
git clone https://github.com/jxxghp/MoviePilot-Resources
```
- 安装后端依赖，设置`app`为源代码根目录，运行 `main.py` 启动后端服务，默认监听端口：`3001`，API文档地址：`http://localhost:3001/docs`
```shell
pip install -r requirements.txt
python3 main.py
```
- 克隆前端项目 [MoviePilot-Frontend](https://github.com/jxxghp/MoviePilot-Frontend)
```shell
git clone https://github.com/jxxghp/MoviePilot-Frontend
```
- 安装前端依赖，运行前端项目，访问：`http://localhost:5173`
```shell
yarn
yarn dev
```
- 参考 [插件开发指引](https://wiki.movie-pilot.org/zh/plugindev) 在 `app/plugins` 目录下开发插件代码

## 相关项目

- [MoviePilot-Frontend](https://github.com/jxxghp/MoviePilot-Frontend)
- [MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources)
- [MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)
- [MoviePilot-Server](https://github.com/jxxghp/MoviePilot-Server)
- [MoviePilot-Wiki](https://github.com/jxxghp/MoviePilot-Wiki)

## 免责申明

- 本软件仅供学习交流使用，任何人不得将本软件用于商业用途，任何人不得将本软件用于违法犯罪活动，软件对用户行为不知情，一切责任由使用者承担。
- 本软件代码开源，基于开源代码进行修改，人为去除相关限制导致软件被分发、传播并造成责任事件的，需由代码修改发布者承担全部责任，不建议对用户认证机制进行规避或修改并公开发布。
- 本项目不接受捐赠，没有在任何地方发布捐赠信息页面，软件本身不收费也不提供任何收费相关服务，请仔细辨别避免误导。

## 贡献者

<a href="https://github.com/jxxghp/MoviePilot/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=jxxghp/MoviePilot" />
</a>
