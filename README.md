# MoviePilot

基于 [NAStool](https://github.com/NAStool/nas-tools) 部分代码重新设计，聚焦自动化核心需求，减少问题同时更易于扩展和维护。

# 仅用于学习交流使用，请勿在任何国内平台宣传该项目！

Docker：https://hub.docker.com/r/jxxghp/moviepilot

发布频道：https://t.me/moviepilot_channel

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

- **NGINX_PORT：** WEB服务端口，默认`3000`，可自行修改，但不能为`3001`
- **SUPERUSER：** 超级管理员用户名，默认`admin`，安装后使用该用户登录后台管理界面
- **SUPERUSER_PASSWORD：** 超级管理员初始密码，默认`password`，建议修改为复杂密码
- **API_TOKEN：** API密钥，默认`moviepilot`，在媒体服务器Webhook、微信回调等地址配置中需要加上`?token=`该值，建议修改为复杂字符串
- **PROXY_HOST：** 网络代理（可选），访问themoviedb需要使用代理访问，格式为`http(s)://ip:port`
- **TMDB_API_DOMAIN：** TMDB API地址，默认`api.themoviedb.org`，也可配置为`api.tmdb.org`或其它中转代理服务地址，能连通即可
- **DOWNLOAD_PATH：** 下载保存目录，**注意：需要将`moviepilot`及`下载器`的映射路径保持一致**，否则会导致下载文件无法转移
- **DOWNLOAD_MOVIE_PATH：** 电影下载保存目录，**必须是DOWNLOAD_PATH的下级路径**，不设置则下载到DOWNLOAD_PATH
- **DOWNLOAD_TV_PATH：** 电视剧下载保存目录，**必须是DOWNLOAD_PATH的下级路径**，不设置则下载到DOWNLOAD_PATH
- **DOWNLOAD_CATEGORY：** 下载二级分类开关，`true`/`false`，默认`false`，开启后会根据配置`category.yaml`自动在下载目录下建立二级目录分类
- **DOWNLOAD_SUBTITLE：** 下载站点字幕，`true`/`false`，默认`true`
- **REFRESH_MEDIASERVER：** 入库刷新媒体库，`true`/`false`，默认`true`
- **SCRAP_METADATA：** 刮削入库的媒体文件，`true`/`false`，默认`true`
- **TORRENT_TAG：** 种子标签，默认为`MOVIEPILOT`，设置后只有MoviePilot添加的下载才会处理，留空所有下载器中的任务均会处理
- **LIBRARY_PATH：** 媒体库目录，多个目录使用`,`分隔
- **LIBRARY_MOVIE_NAME：** 电影媒体库目录名，默认`电影`
- **LIBRARY_TV_NAME：** 电视剧媒体库目录名，默认`电视剧`
- **LIBRARY_CATEGORY：** 媒体库二级分类开关，`true`/`false`，默认`false`，开启后会根据配置`category.yaml`自动在媒体库目录下建立二级目录分类
- **TRANSFER_TYPE：** 转移方式，支持`link`/`copy`/`move`/`softlink`
- **COOKIECLOUD_HOST：** CookieCloud服务器地址，格式：`http://ip:port`，必须配置，否则无法添加站点
- **COOKIECLOUD_KEY：** CookieCloud用户KEY
- **COOKIECLOUD_PASSWORD：** CookieCloud端对端加密密码
- **COOKIECLOUD_INTERVAL：** CookieCloud同步间隔（分钟）
- **USER_AGENT：** CookieCloud对应的浏览器UA，可选，设置后可增加连接站点的成功率，同步站点后可以在管理界面中修改


**MESSAGER：** 消息通知渠道，支持 `telegram`/`wechat`/`slack`，开启多个渠道时使用`,`分隔。同时还需要配置对应渠道的环境变量，非对应渠道的变量可删除，推荐使用`telegram`

`wechat`设置项：

- **WECHAT_CORPID：** WeChat企业ID
- **WECHAT_APP_SECRET：** WeChat应用Secret
- **WECHAT_APP_ID：** WeChat应用ID
- **WECHAT_TOKEN：** WeChat消息回调的Token
- **WECHAT_ENCODING_AESKEY：** WeChat消息回调的EncodingAESKey
- **WECHAT_ADMINS：** WeChat管理员列表，多个管理员用英文逗号分隔（可选）
- **WECHAT_PROXY：** WeChat代理服务器（后面不要加/）

`telegram`设置项：

- **TELEGRAM_TOKEN：** Telegram Bot Token
- **TELEGRAM_CHAT_ID：** Telegram Chat ID
- **TELEGRAM_USERS：** Telegram 用户ID，多个使用,分隔，只有用户ID在列表中才可以使用Bot，如未设置则均可以使用Bot
- **TELEGRAM_ADMINS：** Telegram 管理员ID，多个使用,分隔，只有管理员才可以操作Bot菜单，如未设置则均可以操作菜单

`slack`设置项：

- **SLACK_OAUTH_TOKEN：** Slack Bot User OAuth Token
- **SLACK_APP_TOKEN：** Slack App-Level Token
- **SLACK_CHANNEL：** Slack 频道名称，默认`全体`


**DOWNLOADER：** 下载器，支持`qbittorrent`/`transmission`，QB版本号要求>= 4.3.9，TR版本号要求>= 3.0，同时还需要配置对应渠道的环境变量，非对应渠道的变量可删除，推荐使用`qbittorrent`

`qbittorrent`设置项：

- **QB_HOST：** qbittorrent地址，格式：`ip:port`，https需要添加`https://`前缀
- **QB_USER：** qbittorrent用户名
- **QB_PASSWORD：** qbittorrent密码

`transmission`设置项：

- **TR_HOST：** transmission地址，格式：`ip:port`，https需要添加`https://`前缀
- **TR_USER：** transmission用户名
- **TR_PASSWORD：** transmission密码

**MEDIASERVER：** 媒体服务器，支持`emby`/`jellyfin`/`plex`，同时还需要配置对应媒体服务器的环境变量，非对应媒体服务器的变量可删除，推荐使用`emby`

**MEDIASERVER_SYNC_INTERVAL:** 媒体服务器同步间隔（小时），默认`6`，留空则不同步

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

`MoviePilot`需要认证后才能使用，配置`AUTH_SITE`后，需要根据下表配置对应站点的认证参数。

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


### 2. **进阶配置**

- **BIG_MEMORY_MODE：** 大内存模式，默认为`false`，开启后会占用更多的内存，但响应速度会更快

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


### 3. **过滤规则**

在`设定`-`规则`中设定，规则说明：

- 仅支持使用内置规则进行排列组合，内置规则有：`蓝光原盘`、`4K`、`1080P`、`中文字幕`、`特效字幕`、`H265`、`H264`、`杜比`、`HDR`、`REMUX`、`WEB-DL`、`免费`、`国语配音` 等
- 符合任一层级规则的资源将被标识选中，匹配成功的层级做为该资源的优先级，排越前面优先级超高
- 不符合过滤规则所有层级规则的资源将不会被选中


## 使用

- 通过CookieCloud同步快速同步站点，不需要使用的站点可在WEB管理界面中禁用。
- 通过下载器监控实现自动整理入库刮削。
- 通过微信/Telegram/Slack远程管理，其中Telegram将会自动添加操作菜单。微信回调相对路径为`/api/v1/message/`。
- 通过WEB进行管理，将WEB添加到手机桌面获得类App使用效果，管理界面端口：`3000`。
- 设置媒体服务器Webhook，通过MoviePilot发送播放通知等。Webhook回调相对路径为`/api/v1/webhook?token=moviepilot`，其中`moviepilot`为设置的`API_TOKEN`。
- 将MoviePilot做为Radarr或Sonarr服务器添加到Overseerr或Jellyseerr，可使用Overseerr/Jellyseerr浏览订阅。

**注意**

1) 容器首次启动需要下载浏览器内核，根据网络情况可能需要较长时间，此时无法登录。可映射`/moviepilot`目录避免容器重置后重新触发浏览器内核下载。
2) 使用反向代理时，需要添加以下配置，否则可能会导致部分功能无法访问（`ip:port`修改为实际值）：
```nginx configuration
location / {
    proxy_pass http://ip:port;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/b8f0238d-847f-4f9d-b210-e905837362b9)

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/28219233-ec7d-479b-b184-9a901c947dd1)

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/f7df0806-668d-4c8b-ad41-133bf8f0bf73)

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/f7ea77cd-0362-4c35-967c-7f1b22dbef05)
