# MoviePilot

基于 [NAStool](https://github.com/NAStool/nas-tools) 部分代码重新设计，聚焦自动化核心需求，减少问题同时更易于扩展和维护。

Docker：https://hub.docker.com/r/jxxghp/moviepilot

## 主要特性
- 前后端分离，基于FastApi + Vue3，前端项目地址：[MoviePilot-Frontend](https://github.com/jxxghp/MoviePilot-Frontend)
- 聚焦核心需求，简化功能和设置，部分设置项可直接使用默认值。
- 重新设计了用户界面，更加美观易用。

## 安装

1. **安装CookieCloud插件**

站点信息需要通过CookieCloud同步获取，因此需要安装CookieCloud插件，将浏览器中的站点Cookie数据同步到云端后再同步到MoviePilot使用。 插件下载地址请点击 [这里](https://github.com/easychen/CookieCloud/releases)。

2. **安装CookieCloud服务端（可选）**

MoviePilot内置了公共的CookieCloud服务器，如果需要自建服务，可参考 [CookieCloud](https://github.com/easychen/CookieCloud) 项目进行安装。
```shell
docker pull easychen/cookiecloud:latest
```

3. **安装配套管理软件**

MoviePilot跟NAStool一样，需要配套下载器和媒体服务器使用。
- 下载器支持：qBittorrent、Transmission，QB版本号要求>= 4.3.9，TR版本号要求>= 3.0，推荐使用QB。
- 媒体服务器支持：Jellyfin、Emby、Plex，推荐使用Emby。

4. **安装MoviePilot**

目前仅提供docker镜像，后续可能会提供更多安装方式。

```shell
docker pull jxxghp/moviepilot:latest
```

## 配置

项目的所有配置均通过环境变量进行设置，部分环境建立容器后会自动显示待配置项，如未自动显示配置项则需要手动增加对应环境变量。

### 1. **基础设置**

- **HOST：** 监听地址，默认`0.0.0.0`，如需支持ipv6则需改为`::`
- **SUPERUSER：** 超级管理员用户名，默认`admin`，安装后使用该用户登录后台管理界面
- **SUPERUSER_PASSWORD：** 超级管理员初始密码，默认`password`，建议修改为复杂密码
- **API_TOKEN：** API密钥，默认`moviepilot`，在媒体服务器Webhook、微信回调等地址配置中需要加上`?token=`该值，建议修改为复杂字符串
- **PROXY_HOST：** 网络代理（可选），访问themoviedb需要使用代理访问，格式为`http(s)://ip:port`
- **TMDB_API_DOMAIN：** TMDB API地址，默认`api.themoviedb.org`，也可配置为`api.tmdb.org`或其它中转代理服务地址，能连通即可
- **DOWNLOAD_PATH：** 下载保存目录，**注意：需要将`moviepilot`及`下载器`的映射路径与宿主机`真实路径`保持一致**，例如群晖中下载路程径为`/volume1/downloads`，则需要将`moviepilot`及`下载器`的映射路径均设置为`/volume1/downloads`，否则会导致下载文件无法转移
- **LIBRARY_PATH：** 媒体库目录，**注意：需要将`moviepilot`的映射路径与宿主机`真实路径`保持一致**，多个目录使用`,`分隔
- **LIBRARY_CATEGORY：** 二级分类开关，`true`/`false`，开启后会根据配置自动在媒体库目录下建立二级目录分类
- **DOUBAN_USER_IDS：** 豆瓣用户ID，用于同步豆瓣标记的`想看`数据，自动添加订阅，多个用户使用,分隔
- **TRANSFER_TYPE：** 转移方式，支持`link`/`copy`/`move`/`softlink`
- **COOKIECLOUD_HOST：** CookieCloud服务器地址，格式：`http://ip:port`，必须配置，否则无法添加站点
- **COOKIECLOUD_KEY：** CookieCloud用户KEY
- **COOKIECLOUD_PASSWORD：** CookieCloud端对端加密密码
- **COOKIECLOUD_INTERVAL：** CookieCloud同步间隔（分钟）
- **USER_AGENT：** CookieCloud对应的浏览器UA，可选，同步站点后可以在管理界面中修改


- **MESSAGER：** 消息通知渠道，支持 `telegram`/`wechat`/`slack`，开启多个渠道时使用`,`分隔。同时还需要配置对应渠道的环境变量，非对应渠道的变量可删除，推荐使用`telegram`

`wechat`设置项：

- **WECHAT_CORPID：** WeChat企业ID
- **WECHAT_APP_SECRET：** WeChat应用Secret
- **WECHAT_APP_ID：** WeChat应用ID
- **WECHAT_TOKEN：** WeChat消息回调的Token
- **WECHAT_ENCODING_AESKEY：** WeChat消息回调的EncodingAESKey
- **WECHAT_ADMINS：** WeChat管理员列表，多个管理员用英文逗号分隔（可选）

`telegram`设置项：

- **TELEGRAM_TOKEN：** Telegram Bot Token
- **TELEGRAM_CHAT_ID：** Telegram Chat ID
- **TELEGRAM_USERS：** Telegram 用户ID，多个使用,分隔，只有用户ID在列表中才可以使用Bot，如未设置则均可以使用Bot
- **TELEGRAM_ADMINS：** Telegram 管理员ID，多个使用,分隔，只有管理员才可以操作Bot菜单，如未设置则均可以操作菜单

`slack`设置项：

- **SLACK_OAUTH_TOKEN：** Slack Bot User OAuth Token
- **SLACK_APP_TOKEN：** Slack App-Level Token
- **SLACK_CHANNEL：** Slack 频道名称，默认`全体`


- **DOWNLOADER：** 下载器，支持`qbittorrent`/`transmission`，QB版本号要求>= 4.3.9，TR版本号要求>= 3.0，同时还需要配置对应渠道的环境变量，非对应渠道的变量可删除，推荐使用`qbittorrent`

`qbittorrent`设置项：

- **QB_HOST：** qbittorrent地址，格式：`ip:port`，https需要添加`https://`前缀
- **QB_USER：** qbittorrent用户名
- **QB_PASSWORD：** qbittorrent密码

`transmission`设置项：

- **TR_HOST：** transmission地址，格式：`ip:port`，https需要添加`https://`前缀
- **TR_USER：** transmission用户名
- **TR_PASSWORD：** transmission密码

- **MEDIASERVER：** 媒体服务器，支持`emby`/`jellyfin`/`plex`，同时还需要配置对应媒体服务器的环境变量，非对应媒体服务器的变量可删除，推荐使用`emby`

`emby`设置项：

- **EMBY_HOST：** Emby服务器地址，格式：`ip:port`，https需要添加`https://`前缀
- **EMBY_API_KEY：** Emby Api Key，在`设置->高级->API密钥`处生成

`jellyfin`设置项：

- **JELLYFIN_HOST：** Jellyfin服务器地址，格式：`ip:port`，https需要添加`https://`前缀
- **JELLYFIN_API_KEY：** Jellyfin Api Key，在`设置->高级->API密钥`处生成

`plex`设置项：
 
 - **PLEX_HOST：** Plex服务器地址，格式：`ip:port`，https需要添加`https://`前缀
 - **PLEX_TOKEN：** Plex网页Url中的`X-Plex-Token`，通过浏览器F12->网络从请求URL中获取


### 2. **用户认证**

- **AUTH_SITE：** 认证站点，支持`hhclub`/`audiences`/`hddolby`/`zmpt`/`freefarm`/`hdfans`/`wintersakura`/`leaves`/`1ptba`/`icc2022`/`iyuu`

`MoviePilot`为了控制用户数量，同样需要认证PT用户后才能使用，配置`AUTH_SITE`后，需要根据下表配置对应站点的认证参数。

| 站点 | 参数                                                    |
|----|-------------------------------------------------------|
| iyuu | `IYUU_SIGN`：IYUU登录令牌                                  |
| hhclub | `HHCLUB_USERNAME`：用户名<br/>`HHCLUB_PASSKEY`：密钥         |
| audiences | `AUDIENCES_UID`：用户ID<br/>`AUDIENCES_PASSKEY`：密钥       |
| hddolby | `HDDOLBY_ID`：用户ID<br/>`HDDOLBY_PASSKEY`：密钥             |
| zmpt | `ZMPT_UID`：用户ID<br/>`ZMPT_PASSKEY`：密钥                 |
| freefarm | `FREEFARM_UID`：用户ID<br/>`FREEFARM_PASSKEY`：密钥         |
| hdfans | `HDFANS_UID`：用户ID<br/>`HDFANS_PASSKEY`：密钥             |
| wintersakura | `WINTERSAKURA_UID`：用户ID<br/>`WINTERSAKURA_PASSKEY`：密钥 |
| leaves | `LEAVES_UID`：用户ID<br/>`LEAVES_UID`：密钥                 |
| 1ptba | `1PTBA_UID`：用户ID<br/>`1PTBA_PASSKEY`：密钥               |
| icc2022 | `ICC2022_UID`：用户ID<br/>`ICC2022_PASSKEY`：密钥           |


### 3. **过滤规则**

- **FILTER_RULE：** 配置过规则，默认`!BLU & 4K & CN > !BLU & 1080P & CN > !BLU & 4K > !BLU & 1080P` 表示优先中文字幕非蓝光4K，然后中文字幕非蓝光1080P，然后非蓝光4K，最后非蓝光1080P

`FILTER_RULE` 规则说明：

- 仅支持使用内置规则进行排列组合，内置规则有：`BLU`、`4K`、`1080P`、`CN`、`H265`、`H264`、`DOLBY`、`HDR`、`REMUX`、`WEB-DL`、`FREE`
- `&`表示与，`｜`表示或，`!`表示非，`>`表示优先级层级
- 符合任一层级规则的资源将被标识选中，匹配成功的层级做为该资源的优先级，排越前面优先级超高
- 不符合过滤规则所有层级规则的资源将不会被选中

### 3. **进阶配置**

- **MOVIE_RENAME_FORMAT：** 电影重命名格式

`MOVIE_RENAME_FORMAT`支持的配置项：

> `title`： 标题  
> `original_name`： 原文件名  
> `original_title`： 原语种标题  
> `name`： 识别名称  
> `year`： 年份  
> `edition`： 版本  
> `videoFormat`： 分辨率  
> `releaseGroup`： 制作组/字幕组  
> `effect`： 特效  
> `videoCodec`： 视频编码  
> `audioCodec`： 音频编码  
> `tmdbid`： TMDBID  
> `imdbid`： IMDBID  
> `part`：段/节  
> `fileExt`：文件扩展名

`MOVIE_RENAME_FORMAT`默认配置格式：

```
{{title}}{% if year %} ({{year}}){% endif %}/{{title}}{% if year %} ({{year}}){% endif %}{% if part %}-{{part}}{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}{{fileExt}}
```

- **TV_RENAME_FORMAT：** 电视剧重命名格式

`TV_RENAME_FORMAT`额外支持的配置项：

> `season`： 季号  
> `episode`： 集号  
> `season_episode`： 季集 SxxExx  

`TV_RENAME_FORMAT`默认配置格式：

```
{{title}}{% if year %} ({{year}}){% endif %}/Season {{season}}/{{title}} - {{season_episode}}{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif %}{{fileExt}}
```

## 使用

- 通过CookieCloud同步快速同步站点，不需要使用的站点可在WEB管理界面中禁用。
- 通过下载器监控实现资源下载后自动整理入库刮削。
- 通过微信/Telegram/Slack远程搜索下载、订阅和管理设置，其中Telegram将会自动添加操作菜单。微信回调相对路径为`/api/v1/message/`。
- 通过WEB进行管理，将WEB添加到手机桌面获得类App使用效果，管理界面端口：`3000`。
- 设置媒体服务器Webhook，通过MoviePilot发送播放通知，以及后续播放限速等插件功能。Webhook回调相对路径为`/api/v1/webhook?token=moviepilot`，其中`moviepilot`为设置的`API_TOKEN`。
- 将MoviePilot做为Radarr或Sonarr服务器添加到Overseerr或Jellyseerr，可使用Overseerr/Jellyseerr选片。

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/b8f0238d-847f-4f9d-b210-e905837362b9)

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/28219233-ec7d-479b-b184-9a901c947dd1)

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/f7df0806-668d-4c8b-ad41-133bf8f0bf73)

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/f7ea77cd-0362-4c35-967c-7f1b22dbef05)



## TODO

仍在开发中，当前功能并不完善。

- [x] 搜索结果过滤
- [x] 多通知渠道支持
- [x] 多媒体库目录支持
- [ ] 插件管理，支持自定义插件功能界面
- [x] 自定义识别词
- [ ] 手动整理功能增强
- [ ] 消息中心、工具中心
- [ ] 本地存在标识
- [ ] 媒体详情页面
- [ ] 洗版支持

