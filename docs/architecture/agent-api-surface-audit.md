# MoviePilot Agent API Surface Audit

> Generated from the v1 FastAPI OpenAPI document and the fixed Agent API registry.
> Do not edit route rows manually; run `scripts/generate_agent_api_surface_audit.py`.

## Result

- OpenAPI HTTP operations: **392**
- Stable `moviepilot_api` operations: **205**
- Exact HTTP routes used by the gateway: **203**
- OpenAPI routes matched directly by the gateway: **202**
- Bounded dynamic gateway routes: **1**
- Every gateway operation has a generated English oneOf input contract in MCP `tools/list` and `skills/moviepilot-api/SKILL.md`.
- Every non-gateway OpenAPI operation is listed below with an explicit ownership boundary; it is not silently callable through arbitrary URL/method input.

## Dispositions

| disposition | count | meaning |
| :--- | ---: | :--- |
| `alternate-auth-duplicate` | 11 | API-token compatibility duplicate of a bearer-authenticated capability. |
| `consolidated` | 72 | Source/UI route represented by a stable aggregate Agent operation. |
| `gateway` | 202 | Approved structured MoviePilot Agent operation. |
| `provider-skill` | 11 | Low-level downloader or media-server capability owned by a provider Skill. |
| `stream_or_binary` | 10 | Streaming or binary response owned by a direct client transport. |
| `transport_or_identity` | 66 | Authentication, protocol, callback, account, or conversation transport boundary. |
| `ui_presentation` | 20 | Frontend or plugin-rendered presentation contract. |

## Bounded Dynamic Routes

| method | route template | operations | constraint |
| :--- | :--- | :--- | :--- |
| `GET` | `/api/v1/{source}/person/credits/{person_id}` | media.person.credits | The executor validates and expands this bounded source placeholder to one of tmdb, douban, bangumi, or anilist before calling the corresponding concrete OpenAPI route. |

## Complete Route Inventory

| method | path | tags | disposition | owner / operation | summary |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `GET` | `/api/v1/anilist/credits/{anilist_id}` | anilist | `consolidated` | moviepilot-api | 查询 AniList 配音演员 |
| `GET` | `/api/v1/anilist/discover` | anilist | `consolidated` | moviepilot-api | 探索 AniList 动画 |
| `GET` | `/api/v1/anilist/person/credits/{person_id}` | anilist | `consolidated` | moviepilot-api | 查询 AniList 人物作品 |
| `GET` | `/api/v1/anilist/person/{person_id}` | anilist | `consolidated` | moviepilot-api | 查询 AniList 人物详情 |
| `GET` | `/api/v1/anilist/popular-this-season` | anilist | `consolidated` | moviepilot-api | 查询 AniList 本季热门榜 |
| `GET` | `/api/v1/anilist/recommend/{anilist_id}` | anilist | `consolidated` | moviepilot-api | 查询 AniList 相关推荐 |
| `GET` | `/api/v1/anilist/trending` | anilist | `consolidated` | moviepilot-api | 查询 AniList 当前趋势榜 |
| `GET` | `/api/v1/anilist/{anilist_id}` | anilist | `consolidated` | moviepilot-api | 查询 AniList 动画详情 |
| `POST` | `/api/v1/anthropic/v1/messages` | anthropic | `transport_or_identity` | host-runtime | Anthropic compatible messages |
| `POST` | `/api/v1/auth/exchange` | auth | `transport_or_identity` | host-runtime | 兑换插件认证登录票据 |
| `GET` | `/api/v1/auth/providers` | auth | `transport_or_identity` | host-runtime | 查询登录认证提供方 |
| `GET` | `/api/v1/bangumi/credits/{bangumiid}` | bangumi | `consolidated` | moviepilot-api | 查询Bangumi演职员表 |
| `GET` | `/api/v1/bangumi/person/credits/{person_id}` | bangumi | `consolidated` | moviepilot-api | 人物参演作品 |
| `GET` | `/api/v1/bangumi/person/{person_id}` | bangumi | `consolidated` | moviepilot-api | 人物详情 |
| `GET` | `/api/v1/bangumi/recommend/{bangumiid}` | bangumi | `consolidated` | moviepilot-api | 查询Bangumi推荐 |
| `GET` | `/api/v1/bangumi/{bangumiid}` | bangumi | `consolidated` | moviepilot-api | 查询Bangumi详情 |
| `GET` | `/api/v1/dashboard/cpu` | dashboard | `gateway` | dashboard.cpu | 获取当前CPU使用率 |
| `GET` | `/api/v1/dashboard/cpu2` | dashboard | `alternate-auth-duplicate` | moviepilot-api | 获取当前CPU使用率（API_TOKEN） |
| `GET` | `/api/v1/dashboard/downloader` | dashboard | `gateway` | dashboard.downloader | 下载器信息 |
| `GET` | `/api/v1/dashboard/downloader2` | dashboard | `alternate-auth-duplicate` | moviepilot-api | 下载器信息（API_TOKEN） |
| `GET` | `/api/v1/dashboard/memory` | dashboard | `gateway` | dashboard.memory | 获取当前应用与系统内存信息 |
| `GET` | `/api/v1/dashboard/memory2` | dashboard | `alternate-auth-duplicate` | moviepilot-api | 获取当前应用与系统内存信息（API_TOKEN） |
| `GET` | `/api/v1/dashboard/network` | dashboard | `gateway` | dashboard.network | 获取当前网络流量 |
| `GET` | `/api/v1/dashboard/network2` | dashboard | `alternate-auth-duplicate` | moviepilot-api | 获取当前网络流量（API_TOKEN） |
| `GET` | `/api/v1/dashboard/processes` | dashboard | `gateway` | dashboard.processes | 进程信息 |
| `GET` | `/api/v1/dashboard/schedule` | dashboard | `gateway` | scheduler.list | 后台服务 |
| `GET` | `/api/v1/dashboard/schedule/{job_id}/progress` | dashboard | `gateway` | scheduler.progress | 后台服务进度 |
| `GET` | `/api/v1/dashboard/schedule2` | dashboard | `alternate-auth-duplicate` | moviepilot-api | 后台服务（API_TOKEN） |
| `GET` | `/api/v1/dashboard/schedule2/{job_id}/progress` | dashboard | `alternate-auth-duplicate` | moviepilot-api | 后台服务进度（API_TOKEN） |
| `GET` | `/api/v1/dashboard/statistic` | dashboard | `gateway` | dashboard.media.statistics | 媒体数量统计 |
| `GET` | `/api/v1/dashboard/statistic2` | dashboard | `alternate-auth-duplicate` | moviepilot-api | 媒体数量统计（API_TOKEN） |
| `GET` | `/api/v1/dashboard/storage` | dashboard | `gateway` | dashboard.storage | 本地存储空间 |
| `GET` | `/api/v1/dashboard/storage2` | dashboard | `alternate-auth-duplicate` | moviepilot-api | 本地存储空间（API_TOKEN） |
| `GET` | `/api/v1/dashboard/system` | dashboard | `gateway` | dashboard.system | 系统摘要信息 |
| `GET` | `/api/v1/dashboard/transfer` | dashboard | `gateway` | dashboard.transfer.statistics | 文件整理统计 |
| `GET` | `/api/v1/discover/bangumi` | discover | `consolidated` | moviepilot-api | 探索Bangumi |
| `GET` | `/api/v1/discover/douban_movies` | discover | `consolidated` | moviepilot-api | 探索豆瓣电影 |
| `GET` | `/api/v1/discover/douban_tvs` | discover | `consolidated` | moviepilot-api | 探索豆瓣剧集 |
| `GET` | `/api/v1/discover/source` | discover | `consolidated` | moviepilot-api | 获取探索数据源 |
| `GET` | `/api/v1/discover/tmdb_movies` | discover | `consolidated` | moviepilot-api | 探索TMDB电影 |
| `GET` | `/api/v1/discover/tmdb_tvs` | discover | `consolidated` | moviepilot-api | 探索TMDB剧集 |
| `GET` | `/api/v1/douban/credits/{doubanid}/{type_name}` | douban | `consolidated` | moviepilot-api | 豆瓣演员阵容 |
| `GET` | `/api/v1/douban/person/credits/{person_id}` | douban | `consolidated` | moviepilot-api | 人物参演作品 |
| `GET` | `/api/v1/douban/person/{person_id}` | douban | `consolidated` | moviepilot-api | 人物详情 |
| `GET` | `/api/v1/douban/recommend/{doubanid}/{type_name}` | douban | `consolidated` | moviepilot-api | 豆瓣推荐电影/电视剧 |
| `GET` | `/api/v1/douban/{doubanid}` | douban | `consolidated` | moviepilot-api | 查询豆瓣详情 |
| `GET` | `/api/v1/download/` | download | `gateway` | download.tasks.active | 正在下载 |
| `POST` | `/api/v1/download/` | download | `consolidated` | download.add | 添加下载（含媒体信息） |
| `POST` | `/api/v1/download/add` | download | `gateway` | download.add | 添加下载（不含媒体信息） |
| `GET` | `/api/v1/download/clients` | download | `gateway` | download.clients | 查询可用下载器 |
| `GET` | `/api/v1/download/paths` | download | `gateway` | download.paths | 查询可用下载路径 |
| `GET` | `/api/v1/download/start/{hashString}` | download | `provider-skill` | downloader-operation | 开始任务 |
| `GET` | `/api/v1/download/stop/{hashString}` | download | `provider-skill` | downloader-operation | 暂停任务 |
| `POST` | `/api/v1/download/subtitle` | download | `provider-skill` | downloader-operation | 下载字幕 |
| `DELETE` | `/api/v1/download/{hashString}` | download | `provider-skill` | downloader-operation | 删除下载任务 |
| `PATCH` | `/api/v1/download/{hashString}` | download | `provider-skill` | downloader-operation | 高级更新下载任务 |
| `DELETE` | `/api/v1/history/download` | history | `gateway` | download.history.delete | 删除下载历史记录 |
| `GET` | `/api/v1/history/download` | history | `gateway` | download.history.list | 查询下载历史记录 |
| `DELETE` | `/api/v1/history/transfer` | history | `gateway` | transfer.history.delete | 删除整理记录 |
| `GET` | `/api/v1/history/transfer` | history | `gateway` | transfer.history | 查询整理记录 |
| `POST` | `/api/v1/history/transfer/ai-redo` | history | `gateway` | transfer.history.redo_batch | 智能助手批量重新整理 |
| `DELETE` | `/api/v1/history/transfer/all` | history | `gateway` | transfer.history.clear | 清空旧整理记录 |
| `POST` | `/api/v1/history/transfer/{history_id}/ai-redo` | history | `gateway` | transfer.history.redo | 智能助手重新整理 |
| `POST` | `/api/v1/llm/manage` | llm | `transport_or_identity` | host-runtime | LLM提供商统一管理 |
| `GET` | `/api/v1/llm/provider-auth/callback/{provider_id}` | llm | `transport_or_identity` | host-runtime | LLM提供商OAuth回调 |
| `POST` | `/api/v1/login/access-token` | login | `transport_or_identity` | host-runtime | 获取token |
| `GET` | `/api/v1/login/initialization` | login | `transport_or_identity` | host-runtime | 查询首次初始化状态 |
| `POST` | `/api/v1/login/initialization` | login | `transport_or_identity` | host-runtime | 完成首次初始化 |
| `GET` | `/api/v1/login/wallpaper` | login | `transport_or_identity` | host-runtime | 登录页面电影海报 |
| `GET` | `/api/v1/login/wallpapers` | login | `transport_or_identity` | host-runtime | 登录页面电影海报列表 |
| `DELETE` | `/api/v1/mcp` | mcp | `transport_or_identity` | host-runtime | 终止 MCP 会话 |
| `POST` | `/api/v1/mcp` | mcp | `transport_or_identity` | host-runtime | MCP JSON-RPC 端点 |
| `GET` | `/api/v1/mcp/tools` | mcp | `transport_or_identity` | host-runtime | 列出所有可用工具 |
| `POST` | `/api/v1/mcp/tools/call` | mcp | `transport_or_identity` | host-runtime | 调用工具 |
| `GET` | `/api/v1/mcp/tools/{tool_name}` | mcp | `transport_or_identity` | host-runtime | 获取工具详情 |
| `GET` | `/api/v1/mcp/tools/{tool_name}/schema` | mcp | `transport_or_identity` | host-runtime | 获取工具参数Schema |
| `GET` | `/api/v1/media/category` | media | `gateway` | media.categories | 查询自动分类配置 |
| `GET` | `/api/v1/media/category/config` | media | `gateway` | media.category.config.get | 获取分类策略配置 |
| `GET` | `/api/v1/media/classification/fields` | media | `ui_presentation` | host-ui | 读取媒体分类字段能力目录 |
| `GET` | `/api/v1/media/classification/history` | media | `ui_presentation` | host-ui | 读取媒体分类策略历史 |
| `POST` | `/api/v1/media/classification/impact` | media | `ui_presentation` | host-ui | 分析分类策略对近期样本的估算影响 |
| `GET` | `/api/v1/media/classification/policy` | media | `ui_presentation` | host-ui | 读取当前媒体分类策略 |
| `PUT` | `/api/v1/media/classification/policy` | media | `ui_presentation` | host-ui | 校验并发布媒体分类策略 |
| `POST` | `/api/v1/media/classification/preview` | media | `ui_presentation` | host-ui | 预览媒体分类策略命中过程 |
| `POST` | `/api/v1/media/classification/rollback/{revision}` | media | `ui_presentation` | host-ui | 把历史媒体分类策略发布为新版本 |
| `POST` | `/api/v1/media/classification/validate` | media | `ui_presentation` | host-ui | 校验媒体分类策略草稿 |
| `GET` | `/api/v1/media/group/seasons/{episode_group}` | media | `gateway` | media.episode_group.seasons | 查询剧集组季信息 |
| `GET` | `/api/v1/media/groups/{tmdbid}` | media | `gateway` | media.episode_groups | 查询媒体剧集组 |
| `GET` | `/api/v1/media/recognize` | media | `gateway` | media.recognize | 识别媒体信息（种子） |
| `GET` | `/api/v1/media/recognize2` | media | `alternate-auth-duplicate` | moviepilot-api | 识别种子媒体信息（API_TOKEN） |
| `GET` | `/api/v1/media/recognize_file` | media | `gateway` | media.recognize_file | 识别媒体信息（文件） |
| `GET` | `/api/v1/media/recognize_file2` | media | `alternate-auth-duplicate` | moviepilot-api | 识别文件媒体信息（API_TOKEN） |
| `POST` | `/api/v1/media/scrape/{storage}` | media | `gateway` | media.scrape | 刮削媒体信息 |
| `GET` | `/api/v1/media/search` | media | `gateway` | media.person.search, media.search | 搜索媒体/人物信息 |
| `GET` | `/api/v1/media/seasons` | media | `gateway` | media.seasons | 查询媒体季信息 |
| `GET` | `/api/v1/media/source` | media | `gateway` | media.sources | 获取媒体数据源 |
| `GET` | `/api/v1/media/{media_id}` | media | `gateway` | media.detail | 查询媒体详情 |
| `GET` | `/api/v1/mediaserver/clients` | mediaserver | `provider-skill` | mediaserver-operation | 查询可用媒体服务器 |
| `GET` | `/api/v1/mediaserver/exists` | mediaserver | `gateway` | library.exists | 查询本地是否存在（数据库） |
| `POST` | `/api/v1/mediaserver/exists_remote` | mediaserver | `provider-skill` | mediaserver-operation | 查询已存在的剧集信息（媒体服务器） |
| `GET` | `/api/v1/mediaserver/latest` | mediaserver | `gateway` | library.latest | 最新入库条目 |
| `GET` | `/api/v1/mediaserver/library` | mediaserver | `provider-skill` | mediaserver-operation | 媒体库列表 |
| `POST` | `/api/v1/mediaserver/notexists` | mediaserver | `provider-skill` | mediaserver-operation | 查询媒体库缺失信息（媒体服务器） |
| `GET` | `/api/v1/mediaserver/play/{itemid}` | mediaserver | `provider-skill` | mediaserver-operation | 在线播放 |
| `GET` | `/api/v1/mediaserver/playing` | mediaserver | `provider-skill` | mediaserver-operation | 正在播放条目 |
| `GET` | `/api/v1/message/` | message | `transport_or_identity` | host-runtime | 回调请求验证 |
| `POST` | `/api/v1/message/` | message | `transport_or_identity` | host-runtime | 接收用户消息 |
| `POST` | `/api/v1/message/agent/callback` | agent | `transport_or_identity` | host-runtime | Web 智能助手按钮回调 |
| `GET` | `/api/v1/message/agent/commands` | agent | `gateway` | slash.list | 获取 Web 智能助手可用命令 |
| `POST` | `/api/v1/message/agent/commands/run` | agent | `gateway` | slash.run | 执行 Agent 斜杠命令 |
| `GET` | `/api/v1/message/agent/file/{file_id}` | agent | `transport_or_identity` | host-runtime | 下载 Web 智能助手附件 |
| `GET` | `/api/v1/message/agent/mcp/servers` | agent | `transport_or_identity` | host-runtime | 查询 Agent MCP 服务器配置 |
| `POST` | `/api/v1/message/agent/mcp/servers` | agent | `transport_or_identity` | host-runtime | 保存 Agent MCP 服务器配置 |
| `POST` | `/api/v1/message/agent/mcp/servers/test` | agent | `transport_or_identity` | host-runtime | 测试 Agent MCP 服务器 |
| `GET` | `/api/v1/message/agent/sessions` | agent | `transport_or_identity` | host-runtime | 获取 Agent 历史会话 |
| `DELETE` | `/api/v1/message/agent/sessions/{session_id}` | agent | `transport_or_identity` | host-runtime | 删除 Agent 历史会话 |
| `GET` | `/api/v1/message/agent/sessions/{session_id}` | agent | `transport_or_identity` | host-runtime | 获取 Agent 历史会话详情 |
| `PUT` | `/api/v1/message/agent/sessions/{session_id}/display` | agent | `transport_or_identity` | host-runtime | 保存 Agent 展示会话 |
| `POST` | `/api/v1/message/agent/sessions/{session_id}/stop` | agent | `transport_or_identity` | host-runtime | 停止 Web 智能助手当前任务 |
| `POST` | `/api/v1/message/agent/stream` | agent | `transport_or_identity` | host-runtime | Web智能助手流式对话 |
| `POST` | `/api/v1/message/agent/upload` | agent | `transport_or_identity` | host-runtime | 上传 Web 智能助手附件 |
| `DELETE` | `/api/v1/message/notification` | message | `transport_or_identity` | host-runtime | 清理通知消息 |
| `GET` | `/api/v1/message/notification` | message | `transport_or_identity` | host-runtime | 获取通知消息 |
| `GET` | `/api/v1/message/web` | message | `transport_or_identity` | host-runtime | 获取WEB消息 |
| `POST` | `/api/v1/message/web` | message | `transport_or_identity` | host-runtime | 接收WEB消息 |
| `POST` | `/api/v1/message/webpush/send` | message | `transport_or_identity` | host-runtime | 发送webpush通知 |
| `POST` | `/api/v1/message/webpush/subscribe` | message | `transport_or_identity` | host-runtime | 客户端webpush通知订阅 |
| `POST` | `/api/v1/mfa/otp/disable` | mfa | `transport_or_identity` | host-runtime | 关闭当前用户的 OTP 验证 |
| `POST` | `/api/v1/mfa/otp/generate` | mfa | `transport_or_identity` | host-runtime | 生成 OTP 验证 URI |
| `POST` | `/api/v1/mfa/otp/verify` | mfa | `transport_or_identity` | host-runtime | 绑定并验证 OTP |
| `POST` | `/api/v1/mfa/passkey/authenticate/finish` | mfa | `transport_or_identity` | host-runtime | 完成 PassKey 认证 |
| `POST` | `/api/v1/mfa/passkey/authenticate/start` | mfa | `transport_or_identity` | host-runtime | 开始 PassKey 认证 |
| `POST` | `/api/v1/mfa/passkey/delete` | mfa | `transport_or_identity` | host-runtime | 删除 PassKey |
| `GET` | `/api/v1/mfa/passkey/list` | mfa | `transport_or_identity` | host-runtime | 获取当前用户的 PassKey 列表 |
| `POST` | `/api/v1/mfa/passkey/register/finish` | mfa | `transport_or_identity` | host-runtime | 完成注册 PassKey |
| `POST` | `/api/v1/mfa/passkey/register/start` | mfa | `transport_or_identity` | host-runtime | 开始注册 PassKey |
| `GET` | `/api/v1/music/album/{album_id}` | music | `gateway` | music.album.get | 查询音乐专辑详情 |
| `GET` | `/api/v1/music/album/{album_id}/related` | music | `gateway` | music.album.related | 查询关联音乐专辑 |
| `GET` | `/api/v1/music/artist/{artist_id}` | music | `gateway` | music.artist.get | 查询音乐艺术家详情 |
| `GET` | `/api/v1/music/artist/{artist_id}/albums` | music | `gateway` | music.artist.albums | 查询艺术家的专辑列表 |
| `GET` | `/api/v1/music/artist/{artist_id}/related` | music | `gateway` | music.artist.related | 查询关联艺术家 |
| `DELETE` | `/api/v1/music/cache` | music | `gateway` | music.cache.clear | 清空音乐识别缓存 |
| `GET` | `/api/v1/music/cache` | music | `gateway` | music.cache.get | 查询音乐识别缓存 |
| `DELETE` | `/api/v1/music/cache/{cache_key}` | music | `gateway` | music.cache.delete | 删除指定音乐识别缓存 |
| `GET` | `/api/v1/music/explore` | music | `gateway` | music.explore | 探索音乐 |
| `POST` | `/api/v1/music/recognize` | music | `gateway` | music.recognize | 识别音乐元数据详情 |
| `POST` | `/api/v1/notification/config` | notification | `transport_or_identity` | host-runtime | 保存通知渠道并同步登录缓存 |
| `POST` | `/api/v1/notification/manage` | notification | `transport_or_identity` | host-runtime | 通知渠道统一管理 |
| `POST` | `/api/v1/openai/v1/chat/completions` | openai | `transport_or_identity` | host-runtime | OpenAI compatible chat completions |
| `GET` | `/api/v1/openai/v1/models` | openai | `transport_or_identity` | host-runtime | OpenAI compatible models |
| `POST` | `/api/v1/openai/v1/responses` | openai | `transport_or_identity` | host-runtime | OpenAI compatible responses |
| `GET` | `/api/v1/plugin/` | plugin | `gateway` | plugin.installed, plugin.market | 所有插件 |
| `POST` | `/api/v1/plugin/clone/{plugin_id}` | plugin | `gateway` | plugin.clone | 创建插件分身 |
| `GET` | `/api/v1/plugin/dashboard/meta` | plugin | `ui_presentation` | host-ui | 获取所有插件仪表板元信息 |
| `GET` | `/api/v1/plugin/dashboard/{plugin_id}` | plugin | `ui_presentation` | host-ui | 获取插件仪表板配置 |
| `GET` | `/api/v1/plugin/dashboard/{plugin_id}/{key}` | plugin | `ui_presentation` | host-ui | 获取插件仪表板配置 |
| `GET` | `/api/v1/plugin/file/{plugin_id}/{filepath}` | plugin | `stream_or_binary` | host-transport | 获取插件静态文件 |
| `GET` | `/api/v1/plugin/folders` | plugin | `gateway` | plugin.folders.get | 获取插件文件夹配置 |
| `POST` | `/api/v1/plugin/folders` | plugin | `gateway` | plugin.folders.update | 保存插件文件夹配置 |
| `DELETE` | `/api/v1/plugin/folders/{folder_name}` | plugin | `gateway` | plugin.folder.delete | 删除插件文件夹 |
| `PATCH` | `/api/v1/plugin/folders/{folder_name}` | plugin | `gateway` | plugin.folder.update | 更新插件文件夹 |
| `POST` | `/api/v1/plugin/folders/{folder_name}` | plugin | `gateway` | plugin.folder.create | 创建插件文件夹 |
| `PUT` | `/api/v1/plugin/folders/{folder_name}/plugins` | plugin | `gateway` | plugin.folder.plugins.update | 更新文件夹中的插件 |
| `DELETE` | `/api/v1/plugin/folders/{folder_name}/plugins/{plugin_id}` | plugin | `gateway` | plugin.folder.plugin.remove | 从文件夹移除插件 |
| `PUT` | `/api/v1/plugin/folders/{folder_name}/plugins/{plugin_id}` | plugin | `gateway` | plugin.folder.plugin.assign | 移动插件到文件夹 |
| `GET` | `/api/v1/plugin/form/{plugin_id}` | plugin | `gateway` | plugin.config.get | 获取插件表单页面 |
| `GET` | `/api/v1/plugin/history/{plugin_id}` | plugin | `gateway` | plugin.history | 获取插件更新说明 |
| `GET` | `/api/v1/plugin/install/{plugin_id}` | plugin | `gateway` | plugin.install | 安装插件 |
| `GET` | `/api/v1/plugin/installed` | plugin | `consolidated` | plugin.installed | 已安装插件 |
| `GET` | `/api/v1/plugin/page/{plugin_id}` | plugin | `ui_presentation` | host-ui | 获取插件数据页面 |
| `GET` | `/api/v1/plugin/rating` | plugin | `gateway` | plugin.ratings | 批量查询插件评分 |
| `GET` | `/api/v1/plugin/rating/{plugin_id}` | plugin | `gateway` | plugin.rating | 查询插件评分 |
| `POST` | `/api/v1/plugin/rating/{plugin_id}` | plugin | `gateway` | plugin.rating.submit | 提交插件评分 |
| `GET` | `/api/v1/plugin/releases/{plugin_id}` | plugin | `gateway` | plugin.releases | 获取插件Release版本 |
| `POST` | `/api/v1/plugin/reload/{plugin_id}` | plugin | `gateway` | plugin.reload | 重新加载插件 |
| `GET` | `/api/v1/plugin/remotes` | plugin | `transport_or_identity` | host-runtime | 获取插件联邦组件列表 |
| `GET` | `/api/v1/plugin/reset/{plugin_id}` | plugin | `gateway` | plugin.reset | 重置插件配置及数据 |
| `GET` | `/api/v1/plugin/runtime` | plugin | `gateway` | plugin.runtime.status | 插件运行时收敛状态 |
| `GET` | `/api/v1/plugin/runtime/capabilities` | plugin | `gateway` | plugin.capabilities | 查询插件运行能力 |
| `GET` | `/api/v1/plugin/runtime/{plugin_id}/data` | plugin | `gateway` | plugin.data | 查询插件持久化数据 |
| `GET` | `/api/v1/plugin/runtime/{plugin_id}/data/summary` | plugin | `ui_presentation` | host-ui | 查询插件持久化数据摘要 |
| `GET` | `/api/v1/plugin/sidebar_nav` | plugin | `ui_presentation` | host-ui | 获取插件侧栏导航项 |
| `GET` | `/api/v1/plugin/source/{plugin_id}` | plugin | `gateway` | plugin.source.options | 获取插件来源身份 |
| `POST` | `/api/v1/plugin/source/{plugin_id}` | plugin | `gateway` | plugin.source.change | 切换插件来源 |
| `POST` | `/api/v1/plugin/source/{plugin_id}/install` | plugin | `gateway` | plugin.source.install | 按明确来源安装插件 |
| `GET` | `/api/v1/plugin/source/{plugin_id}/options` | plugin | `consolidated` | plugin.source.options | 获取插件来源候选 |
| `GET` | `/api/v1/plugin/statistic` | plugin | `gateway` | plugin.statistics | 插件安装统计 |
| `DELETE` | `/api/v1/plugin/{plugin_id}` | plugin | `gateway` | plugin.uninstall | 卸载插件 |
| `GET` | `/api/v1/plugin/{plugin_id}` | plugin | `consolidated` | plugin.config.get | 获取插件配置 |
| `PUT` | `/api/v1/plugin/{plugin_id}` | plugin | `gateway` | plugin.config.update | 更新插件配置 |
| `GET` | `/api/v1/recommend/agent` | recommend | `gateway` | recommendation.list | 统一获取 Agent 推荐结果 |
| `GET` | `/api/v1/recommend/bangumi_calendar` | recommend | `consolidated` | moviepilot-api | Bangumi每日放送 |
| `GET` | `/api/v1/recommend/douban_movie_hot` | recommend | `consolidated` | moviepilot-api | 豆瓣热门电影 |
| `GET` | `/api/v1/recommend/douban_movie_top250` | recommend | `consolidated` | moviepilot-api | 豆瓣电影TOP250 |
| `GET` | `/api/v1/recommend/douban_movies` | recommend | `consolidated` | moviepilot-api | 豆瓣电影 |
| `GET` | `/api/v1/recommend/douban_showing` | recommend | `consolidated` | moviepilot-api | 豆瓣正在热映 |
| `GET` | `/api/v1/recommend/douban_tv_animation` | recommend | `consolidated` | moviepilot-api | 豆瓣动画剧集 |
| `GET` | `/api/v1/recommend/douban_tv_hot` | recommend | `consolidated` | moviepilot-api | 豆瓣热门电视剧 |
| `GET` | `/api/v1/recommend/douban_tv_weekly_chinese` | recommend | `consolidated` | moviepilot-api | 豆瓣国产剧集周榜 |
| `GET` | `/api/v1/recommend/douban_tv_weekly_global` | recommend | `consolidated` | moviepilot-api | 豆瓣全球剧集周榜 |
| `GET` | `/api/v1/recommend/douban_tvs` | recommend | `consolidated` | moviepilot-api | 豆瓣剧集 |
| `GET` | `/api/v1/recommend/music_douban` | recommend | `consolidated` | moviepilot-api | 豆瓣音乐推荐 |
| `GET` | `/api/v1/recommend/music_weekly` | recommend | `consolidated` | moviepilot-api | ListenBrainz 本周热门音乐 |
| `GET` | `/api/v1/recommend/source` | recommend | `consolidated` | moviepilot-api | 获取推荐数据源 |
| `GET` | `/api/v1/recommend/tmdb_movies` | recommend | `consolidated` | moviepilot-api | TMDB电影 |
| `GET` | `/api/v1/recommend/tmdb_trending` | recommend | `consolidated` | moviepilot-api | TMDB流行趋势 |
| `GET` | `/api/v1/recommend/tmdb_tvs` | recommend | `consolidated` | moviepilot-api | TMDB剧集 |
| `GET` | `/api/v1/rule/builtin` | rule | `gateway` | filter.builtin | 查询内置过滤规则 |
| `GET` | `/api/v1/rule/custom` | rule | `gateway` | filter.custom | 查询自定义过滤规则 |
| `POST` | `/api/v1/rule/custom` | rule | `gateway` | filter.custom.add | 新增自定义过滤规则 |
| `PUT` | `/api/v1/rule/custom/reorder` | rule | `ui_presentation` | host-ui | 调整自定义过滤规则顺序 |
| `DELETE` | `/api/v1/rule/custom/{rule_id}` | rule | `gateway` | filter.custom.delete | 删除自定义过滤规则 |
| `PUT` | `/api/v1/rule/custom/{rule_id}` | rule | `gateway` | filter.custom.update | 更新自定义过滤规则 |
| `GET` | `/api/v1/rule/groups` | rule | `gateway` | filter.groups | 查询过滤规则组 |
| `POST` | `/api/v1/rule/groups` | rule | `gateway` | filter.group.add | 新增过滤规则组 |
| `PUT` | `/api/v1/rule/groups/reorder` | rule | `ui_presentation` | host-ui | 调整过滤规则组顺序 |
| `DELETE` | `/api/v1/rule/groups/{name}` | rule | `gateway` | filter.group.delete | 删除过滤规则组 |
| `PUT` | `/api/v1/rule/groups/{name}` | rule | `gateway` | filter.group.update | 更新过滤规则组 |
| `GET` | `/api/v1/search/last` | search | `consolidated` | search.results | 查询搜索结果 |
| `GET` | `/api/v1/search/last/context` | search | `gateway` | search.results | 查询上次搜索上下文 |
| `GET` | `/api/v1/search/media/{media_id}` | search | `gateway` | search.torrents | 精确搜索资源 |
| `GET` | `/api/v1/search/media/{media_id}/stream` | search | `consolidated` | search.torrents | 渐进式精确搜索资源 |
| `POST` | `/api/v1/search/recommend` | search | `gateway` | search.recommend | AI推荐资源 |
| `GET` | `/api/v1/search/subtitle/media/{media_id}` | search | `gateway` | subtitle.search.media | 精确搜索字幕 |
| `GET` | `/api/v1/search/subtitle/media/{media_id}/stream` | search | `consolidated` | subtitle.search.media | 渐进式精确搜索字幕 |
| `GET` | `/api/v1/search/subtitle/title` | search | `gateway` | subtitle.search.title | 模糊搜索字幕 |
| `GET` | `/api/v1/search/subtitle/title/stream` | search | `consolidated` | subtitle.search.title | 渐进式模糊搜索字幕 |
| `GET` | `/api/v1/search/title` | search | `gateway` | search.title | 模糊搜索资源 |
| `GET` | `/api/v1/search/title/stream` | search | `consolidated` | search.title | 渐进式模糊搜索资源 |
| `GET` | `/api/v1/site/` | site | `consolidated` | site.list | 所有站点 |
| `POST` | `/api/v1/site/` | site | `gateway` | site.add | 新增站点 |
| `PUT` | `/api/v1/site/` | site | `gateway` | site.update | 更新站点 |
| `GET` | `/api/v1/site/agent` | site | `gateway` | site.list | 查询 Agent 可用站点 |
| `GET` | `/api/v1/site/auth` | site | `gateway` | site.auth.options | 查询认证站点 |
| `POST` | `/api/v1/site/auth` | site | `gateway` | site.authenticate | 用户站点认证 |
| `GET` | `/api/v1/site/category/{site_id}` | site | `gateway` | site.category | 站点分类 |
| `GET` | `/api/v1/site/cookie/{site_id}` | site | `consolidated` | site.cookie.update | 更新站点Cookie&UA |
| `POST` | `/api/v1/site/cookie/{site_id}` | site | `gateway` | site.cookie.update | 更新站点Cookie&UA |
| `POST` | `/api/v1/site/cookiecloud` | site | `gateway` | site.cookiecloud.sync | CookieCloud同步 |
| `GET` | `/api/v1/site/domain/{site_url}` | site | `consolidated` | site.list | 站点详情 |
| `GET` | `/api/v1/site/icon/{site_id}` | site | `stream_or_binary` | host-transport | 站点图标 |
| `GET` | `/api/v1/site/mapping` | site | `gateway` | site.mapping | 获取站点域名到名称的映射 |
| `GET` | `/api/v1/site/media/{media_type}` | site | `gateway` | site.searchable | 按媒体类型获取可搜索站点 |
| `POST` | `/api/v1/site/priorities` | site | `gateway` | site.priorities.update | 批量更新站点优先级 |
| `POST` | `/api/v1/site/reset` | site | `gateway` | site.reset | 重置站点 |
| `GET` | `/api/v1/site/resource/{site_id}` | site | `gateway` | site.resource | 站点资源 |
| `GET` | `/api/v1/site/rss` | site | `gateway` | site.rss | 所有订阅站点 |
| `GET` | `/api/v1/site/statistic` | site | `gateway` | site.statistics | 所有站点统计信息 |
| `GET` | `/api/v1/site/statistic/{site_url}` | site | `gateway` | site.statistic | 特定站点统计信息 |
| `GET` | `/api/v1/site/supporting` | site | `gateway` | site.supporting | 获取支持的站点列表 |
| `GET` | `/api/v1/site/test/{site_id}` | site | `gateway` | site.test | 连接测试 |
| `GET` | `/api/v1/site/userdata/latest` | site | `gateway` | site.userdata.latest | 查询所有站点最新用户数据 |
| `GET` | `/api/v1/site/userdata/{site_id}` | site | `gateway` | site.userdata | 查询某站点用户数据 |
| `POST` | `/api/v1/site/userdata/{site_id}` | site | `gateway` | site.userdata.refresh | 更新站点用户数据 |
| `DELETE` | `/api/v1/site/{site_id}` | site | `gateway` | site.delete | 删除站点 |
| `GET` | `/api/v1/site/{site_id}` | site | `consolidated` | site.list | 站点详情 |
| `POST` | `/api/v1/storage/agent/list` | storage | `gateway` | storage.list | 查询 Agent 可用目录和文件 |
| `POST` | `/api/v1/storage/delete` | storage | `gateway` | storage.delete | 删除文件或目录 |
| `GET` | `/api/v1/storage/directories` | storage | `gateway` | storage.settings | 查询目录配置 |
| `POST` | `/api/v1/storage/download` | storage | `stream_or_binary` | host-transport | 下载文件 |
| `POST` | `/api/v1/storage/image` | storage | `stream_or_binary` | host-transport | 预览图片 |
| `POST` | `/api/v1/storage/list` | storage | `consolidated` | storage.list | 所有目录和文件 |
| `POST` | `/api/v1/storage/manage` | storage | `gateway` | storage.manage | 网盘存储统一管理 |
| `POST` | `/api/v1/storage/mkdir` | storage | `gateway` | storage.mkdir | 创建目录 |
| `GET` | `/api/v1/storage/options` | storage | `ui_presentation` | host-ui | 查询可用存储选项 |
| `POST` | `/api/v1/storage/rename` | storage | `gateway` | storage.rename | 重命名文件或目录 |
| `GET` | `/api/v1/subscribe/` | subscribe | `gateway` | subscription.list | 查询所有订阅 |
| `POST` | `/api/v1/subscribe/` | subscribe | `gateway` | subscription.add | 新增订阅 |
| `PUT` | `/api/v1/subscribe/` | subscribe | `gateway` | subscription.update | 更新订阅 |
| `POST` | `/api/v1/subscribe/check` | subscribe | `gateway` | subscription.metadata.refresh | 刷新订阅 TMDB 信息 |
| `GET` | `/api/v1/subscribe/execution/batches` | subscribe | `ui_presentation` | host-ui | 查看订阅搜索进度 |
| `GET` | `/api/v1/subscribe/execution/batches/{batch_id}` | subscribe | `ui_presentation` | host-ui | 查看一次订阅搜索 |
| `PUT` | `/api/v1/subscribe/execution/batches/{batch_id}/cancel` | subscribe | `ui_presentation` | host-ui | 停止一次订阅搜索 |
| `GET` | `/api/v1/subscribe/files/{subscribe_id}` | subscribe | `gateway` | subscription.files | 订阅相关文件信息 |
| `DELETE` | `/api/v1/subscribe/follow` | subscribe | `gateway` | subscription.follow.delete | 取消Follow订阅分享人 |
| `GET` | `/api/v1/subscribe/follow` | subscribe | `gateway` | subscription.follow.list | 查询已Follow的订阅分享人 |
| `POST` | `/api/v1/subscribe/follow` | subscribe | `gateway` | subscription.follow.add | Follow订阅分享人 |
| `POST` | `/api/v1/subscribe/fork` | subscribe | `gateway` | subscription.fork | 复用订阅 |
| `DELETE` | `/api/v1/subscribe/history/{history_id}` | subscribe | `gateway` | subscription.history.delete | 删除订阅历史 |
| `GET` | `/api/v1/subscribe/history/{mtype}` | subscribe | `gateway` | subscription.history | 查询订阅历史 |
| `GET` | `/api/v1/subscribe/list` | subscribe | `consolidated` | subscription.list | 查询所有订阅（API_TOKEN） |
| `DELETE` | `/api/v1/subscribe/media/{media_id}` | subscribe | `gateway` | subscription.delete_by_media | 删除订阅 |
| `GET` | `/api/v1/subscribe/media/{media_id}` | subscribe | `gateway` | subscription.find | 查询订阅 |
| `GET` | `/api/v1/subscribe/popular` | subscribe | `gateway` | subscription.popular | 热门订阅（基于用户共享数据） |
| `POST` | `/api/v1/subscribe/refresh` | subscribe | `gateway` | subscription.refresh | 刷新订阅 |
| `POST` | `/api/v1/subscribe/reset/{subid}` | subscribe | `gateway` | subscription.reset | 重置订阅 |
| `POST` | `/api/v1/subscribe/search` | subscribe | `gateway` | subscription.search_all | 搜索所有订阅 |
| `POST` | `/api/v1/subscribe/search/{subscribe_id}` | subscribe | `gateway` | subscription.search | 搜索订阅 |
| `POST` | `/api/v1/subscribe/seerr` | subscribe | `transport_or_identity` | host-runtime | OverSeerr/JellySeerr通知订阅 |
| `POST` | `/api/v1/subscribe/share` | subscribe | `gateway` | subscription.share | 分享订阅 |
| `GET` | `/api/v1/subscribe/share/statistics` | subscribe | `gateway` | subscription.share.statistics | 查询订阅分享统计 |
| `DELETE` | `/api/v1/subscribe/share/{share_id}` | subscribe | `gateway` | subscription.share.delete | 删除分享 |
| `GET` | `/api/v1/subscribe/shares` | subscribe | `gateway` | subscription.shares | 查询分享的订阅 |
| `PUT` | `/api/v1/subscribe/status/{subid}` | subscribe | `gateway` | subscription.status.update | 更新订阅状态 |
| `GET` | `/api/v1/subscribe/user/{username}` | subscribe | `gateway` | subscription.user.list | 用户订阅 |
| `DELETE` | `/api/v1/subscribe/{subscribe_id}` | subscribe | `gateway` | subscription.delete | 删除订阅 |
| `GET` | `/api/v1/subscribe/{subscribe_id}` | subscribe | `gateway` | subscription.get | 订阅详情 |
| `GET` | `/api/v1/system/cache/image` | system | `stream_or_binary` | host-transport | 图片缓存 |
| `GET` | `/api/v1/system/database/backups` | system | `gateway` | database.backups.list | 查询受管数据库备份 |
| `POST` | `/api/v1/system/database/backups` | system | `gateway` | database.backups.create | 立即创建数据库备份 |
| `DELETE` | `/api/v1/system/database/backups/{name}` | system | `gateway` | database.backups.delete | 删除受管数据库备份 |
| `POST` | `/api/v1/system/database/backups/{name}/verify` | system | `gateway` | database.backups.verify | 校验受管数据库备份 |
| `GET` | `/api/v1/system/env` | system | `consolidated` | config.system.get | 查询系统配置 |
| `POST` | `/api/v1/system/env` | system | `consolidated` | config.system.update | 更新系统配置 |
| `GET` | `/api/v1/system/global` | system | `consolidated` | config.system.get | 查询非敏感系统设置 |
| `GET` | `/api/v1/system/global/user` | system | `gateway` | config.user.get | 查询用户相关系统设置 |
| `GET` | `/api/v1/system/identifiers` | system | `gateway` | config.identifiers.get | 查询自定义识别词 |
| `POST` | `/api/v1/system/identifiers` | system | `gateway` | config.identifiers.update | 更新自定义识别词 |
| `GET` | `/api/v1/system/img/{proxy}` | system | `stream_or_binary` | host-transport | 图片代理 |
| `GET` | `/api/v1/system/logging` | system | `stream_or_binary` | host-transport | 实时日志 |
| `GET` | `/api/v1/system/logging/download/{name}` | system | `stream_or_binary` | host-transport | 下载日志 |
| `GET` | `/api/v1/system/message` | system | `stream_or_binary` | host-transport | 实时消息 |
| `GET` | `/api/v1/system/modulelist` | system | `gateway` | system.module.list | 查询已加载的模块ID列表 |
| `GET` | `/api/v1/system/moduletest/{moduleid}` | system | `gateway` | system.module.test | 模块可用性测试 |
| `GET` | `/api/v1/system/nettest` | system | `gateway` | system.network.test | 测试网络连通性 |
| `GET` | `/api/v1/system/nettest/targets` | system | `gateway` | system.network.targets | 获取网络测试目标 |
| `GET` | `/api/v1/system/ping` | system | `transport_or_identity` | host-runtime | 服务存活检测 |
| `GET` | `/api/v1/system/progress/{process_type}` | system | `stream_or_binary` | host-transport | 实时进度 |
| `GET` | `/api/v1/system/restart` | system | `gateway` | system.restart | 重启系统 |
| `GET` | `/api/v1/system/ruletest` | system | `gateway` | filter.test | 过滤规则测试 |
| `GET` | `/api/v1/system/runscheduler` | system | `gateway` | scheduler.run | 运行服务 |
| `GET` | `/api/v1/system/runscheduler2` | system | `alternate-auth-duplicate` | moviepilot-api | 运行服务（API_TOKEN） |
| `POST` | `/api/v1/system/setting/PLUGIN_MARKET/sync-wiki` | system | `gateway` | plugin.market.sync_wiki | 从Wiki同步插件市场仓库 |
| `GET` | `/api/v1/system/setting/public/{key}` | system | `gateway` | config.public.get | 查询公开系统设置 |
| `GET` | `/api/v1/system/setting/{key}` | system | `consolidated` | config.system.get | 查询系统设置 |
| `POST` | `/api/v1/system/setting/{key}` | system | `consolidated` | config.system.update | 更新系统设置 |
| `GET` | `/api/v1/system/settings` | system | `gateway` | config.system.get | Discover or read registered system settings |
| `POST` | `/api/v1/system/settings` | system | `gateway` | config.system.update | Update one registered system setting |
| `POST` | `/api/v1/system/update/check` | system | `gateway` | system.update.check | 立即检查系统更新 |
| `POST` | `/api/v1/system/update/download` | system | `gateway` | system.update.download | 后台下载系统更新 |
| `POST` | `/api/v1/system/update/install` | system | `gateway` | system.update.install | 确认重启安装系统更新 |
| `GET` | `/api/v1/system/update/status` | system | `gateway` | system.update.status | 查询系统更新状态 |
| `POST` | `/api/v1/system/upgrade` | system | `gateway` | system.upgrade.dev | Dev 更新并重启系统 |
| `GET` | `/api/v1/system/usage/statistic` | system | `gateway` | system.usage.statistics | 查询安装版本统计报表 |
| `GET` | `/api/v1/system/versions` | system | `gateway` | system.versions | 查询Github所有Release版本 |
| `DELETE` | `/api/v1/tmdb/cache` | tmdb | `consolidated` | moviepilot-api | 清空 TheMovieDb 识别缓存 |
| `GET` | `/api/v1/tmdb/cache` | tmdb | `consolidated` | moviepilot-api | 查询 TheMovieDb 识别缓存 |
| `DELETE` | `/api/v1/tmdb/cache/{cache_key}` | tmdb | `consolidated` | moviepilot-api | 删除指定 TheMovieDb 识别缓存 |
| `GET` | `/api/v1/tmdb/collection/{collection_id}` | tmdb | `consolidated` | moviepilot-api | 系列合集详情 |
| `GET` | `/api/v1/tmdb/credits/{tmdbid}/{type_name}` | tmdb | `consolidated` | moviepilot-api | 演员阵容 |
| `GET` | `/api/v1/tmdb/person/credits/{person_id}` | tmdb | `consolidated` | moviepilot-api | 人物参演作品 |
| `GET` | `/api/v1/tmdb/person/{person_id}` | tmdb | `consolidated` | moviepilot-api | 人物详情 |
| `GET` | `/api/v1/tmdb/recommend/{tmdbid}/{type_name}` | tmdb | `consolidated` | moviepilot-api | 推荐电影/电视剧 |
| `GET` | `/api/v1/tmdb/seasons/{tmdbid}` | tmdb | `consolidated` | moviepilot-api | TMDB所有季 |
| `GET` | `/api/v1/tmdb/similar/{tmdbid}/{type_name}` | tmdb | `consolidated` | moviepilot-api | 类似电影/电视剧 |
| `GET` | `/api/v1/tmdb/{tmdbid}/{season}` | tmdb | `gateway` | media.episode_schedule | TMDB季所有集 |
| `DELETE` | `/api/v1/torrent/cache` | torrent | `gateway` | torrent.cache.clear | 清理种子缓存 |
| `GET` | `/api/v1/torrent/cache` | torrent | `gateway` | torrent.cache.get | 获取种子缓存 |
| `POST` | `/api/v1/torrent/cache/refresh` | torrent | `gateway` | torrent.cache.refresh | 刷新种子缓存 |
| `POST` | `/api/v1/torrent/cache/reidentify/{domain}/{torrent_hash}` | torrent | `gateway` | torrent.cache.reidentify | 重新识别种子 |
| `DELETE` | `/api/v1/torrent/cache/{domain}/{torrent_hash}` | torrent | `gateway` | torrent.cache.delete | 删除指定种子缓存 |
| `POST` | `/api/v1/transfer/episode-format/recommend` | transfer | `gateway` | transfer.episode_format.recommend | 推荐集数定位模板 |
| `POST` | `/api/v1/transfer/manual` | transfer | `gateway` | transfer.file | 手动转移 |
| `POST` | `/api/v1/transfer/manual/history` | transfer | `gateway` | transfer.manual_history | 查询手动转移成功历史 |
| `POST` | `/api/v1/transfer/manual/target-path` | transfer | `gateway` | transfer.target_path | 匹配手动转移目的路径 |
| `GET` | `/api/v1/transfer/name` | transfer | `gateway` | transfer.name | 查询整理后的名称 |
| `GET` | `/api/v1/transfer/now` | transfer | `consolidated` | scheduler.run | 立即执行下载器文件整理 |
| `DELETE` | `/api/v1/transfer/queue` | transfer | `gateway` | transfer.queue.delete | 从整理队列中删除任务 |
| `GET` | `/api/v1/transfer/queue` | transfer | `gateway` | transfer.queue | 查询整理队列 |
| `GET` | `/api/v1/transfer/tasks/manual-reviews` | transfer | `gateway` | transfer.manual_reviews | 分页查询 durable 整理人工复核任务 |
| `GET` | `/api/v1/transfer/tasks/{task_id}/manual-review` | transfer | `gateway` | transfer.manual_review | 查询 durable 整理人工复核详情 |
| `POST` | `/api/v1/transfer/tasks/{task_id}/manual-review` | transfer | `gateway` | transfer.manual_review.resolve | 人工判定整理步骤的外部执行结果 |
| `GET` | `/api/v1/user/` | user | `transport_or_identity` | host-runtime | 所有用户 |
| `POST` | `/api/v1/user/` | user | `transport_or_identity` | host-runtime | 新增用户 |
| `PUT` | `/api/v1/user/` | user | `transport_or_identity` | host-runtime | 更新用户 |
| `POST` | `/api/v1/user/avatar/{user_id}` | user | `transport_or_identity` | host-runtime | 上传用户头像 |
| `GET` | `/api/v1/user/config/{key}` | user | `transport_or_identity` | host-runtime | 查询用户配置 |
| `POST` | `/api/v1/user/config/{key}` | user | `transport_or_identity` | host-runtime | 更新用户配置 |
| `GET` | `/api/v1/user/current` | user | `transport_or_identity` | host-runtime | 当前登录用户信息 |
| `PUT` | `/api/v1/user/current` | user | `transport_or_identity` | host-runtime | 更新当前用户资料 |
| `DELETE` | `/api/v1/user/id/{user_id}` | user | `transport_or_identity` | host-runtime | 删除用户 |
| `DELETE` | `/api/v1/user/name/{user_name}` | user | `transport_or_identity` | host-runtime | 删除用户 |
| `GET` | `/api/v1/user/{username}` | user | `transport_or_identity` | host-runtime | 用户详情 |
| `GET` | `/api/v1/webhook/` | webhook | `transport_or_identity` | host-runtime | Webhook消息响应 |
| `POST` | `/api/v1/webhook/` | webhook | `transport_or_identity` | host-runtime | Webhook消息响应 |
| `GET` | `/api/v1/workflow/` | workflow | `consolidated` | workflow.list | 所有工作流 |
| `POST` | `/api/v1/workflow/` | workflow | `gateway` | workflow.create | 创建工作流 |
| `GET` | `/api/v1/workflow/actions` | workflow | `gateway` | workflow.actions | 所有动作 |
| `GET` | `/api/v1/workflow/agent` | workflow | `gateway` | workflow.list | 查询 Agent 可用工作流 |
| `GET` | `/api/v1/workflow/event_types` | workflow | `gateway` | workflow.event_types | 获取所有事件类型 |
| `POST` | `/api/v1/workflow/fork` | workflow | `gateway` | workflow.fork | 复用工作流 |
| `GET` | `/api/v1/workflow/plugin/actions` | workflow | `gateway` | workflow.plugin.actions | 查询插件动作 |
| `POST` | `/api/v1/workflow/share` | workflow | `gateway` | workflow.share | 分享工作流 |
| `DELETE` | `/api/v1/workflow/share/{share_id}` | workflow | `gateway` | workflow.share.delete | 删除分享 |
| `GET` | `/api/v1/workflow/shares` | workflow | `gateway` | workflow.shares | 查询分享的工作流 |
| `DELETE` | `/api/v1/workflow/{workflow_id}` | workflow | `gateway` | workflow.delete | 删除工作流 |
| `GET` | `/api/v1/workflow/{workflow_id}` | workflow | `gateway` | workflow.get | 工作流详情 |
| `PUT` | `/api/v1/workflow/{workflow_id}` | workflow | `gateway` | workflow.update | 更新工作流 |
| `POST` | `/api/v1/workflow/{workflow_id}/pause` | workflow | `gateway` | workflow.pause | 停用工作流 |
| `POST` | `/api/v1/workflow/{workflow_id}/reset` | workflow | `gateway` | workflow.reset | 重置工作流 |
| `POST` | `/api/v1/workflow/{workflow_id}/run` | workflow | `gateway` | workflow.run | 执行工作流 |
| `POST` | `/api/v1/workflow/{workflow_id}/start` | workflow | `gateway` | workflow.start | 启用工作流 |

## Exposure Rule

Every structured JSON business endpoint is either a stable gateway operation, a provider Skill capability, or an explicitly consolidated compatibility route. Authentication, webhook, stream, binary, and UI-presentation endpoints remain owned by their direct transport or frontend consumer and must not be made recursively callable by the Agent.
