# MoviePilot

基于 [NAStool](https://github.com/NAStool/nas-tools) 部分代码重新设计，聚焦自动化核心需求，减少问题同时更易于扩展和维护。

# 仅用于学习交流使用，请勿在任何国内平台宣传该项目！

发布频道：https://t.me/moviepilot_channel

## 主要特性
- 前后端分离，基于FastApi + Vue3，前端项目地址：[MoviePilot-Frontend](https://github.com/jxxghp/MoviePilot-Frontend)
- 聚焦核心需求，简化功能和设置，部分设置项可直接使用默认值。
- 重新设计了用户界面，更加美观易用。

## 安装

### 1. **安装CookieCloud插件**

站点信息需要通过CookieCloud同步获取，因此需要安装CookieCloud插件，将浏览器中的站点Cookie数据同步到云端后再同步到MoviePilot使用。 插件下载地址请点击 [这里](https://github.com/easychen/CookieCloud/releases)。

### 2. **安装CookieCloud服务端（可选）**

MoviePilot内置了公共CookieCloud服务器，如果需要自建服务，可参考 [CookieCloud](https://github.com/easychen/CookieCloud) 项目进行搭建，docker镜像请点击 [这里](https://hub.docker.com/r/easychen/cookiecloud)。

**声明：** 本项目不会收集用户敏感数据，Cookie同步也是基于CookieCloud项目实现，非本项目提供的能力。技术角度上CookieCloud采用端到端加密，在个人不泄露`用户KEY`和`端对端加密密码`的情况下第三方无法窃取任何用户信息（包括服务器持有者）。如果你不放心，可以不使用公共服务或者不使用本项目，但如果使用后发生了任何信息泄露与本项目无关！

### 3. **安装配套管理软件**

MoviePilot需要配套下载器和媒体服务器配合使用。
- 下载器支持：qBittorrent、Transmission，QB版本号要求>= 4.3.9，TR版本号要求>= 3.0，推荐使用QB。
- 媒体服务器支持：Jellyfin、Emby、Plex，推荐使用Emby。

### 4. **安装MoviePilot**

- Docker镜像

  点击 [这里](https://hub.docker.com/r/jxxghp/moviepilot) 或执行命令：

  ```shell
  docker pull jxxghp/moviepilot:latest
  ```


- Windows

  下载 [MoviePilot.exe](https://github.com/jxxghp/MoviePilot/releases)，双击运行后自动生成配置文件目录。


- 本地运行

  1) 将工程 [MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins) plugins目录下的所有文件复制到`app/plugins`目录
  2) 将工程 [MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources) resources目录下的所有文件复制到`app/helper`目录
  3) 执行命令：`pip install -r requirements.txt` 安装依赖
  4) 执行命令：`python app/main.py` 启动服务

## 配置

项目的所有配置均通过环境变量进行设置，支持两种配置方式：
- 在Docker环境变量部分或Windows系统环境变量中进行参数配置，如未自动显示配置项则需要手动增加对应环境变量。
- 下载 [app.env](https://github.com/jxxghp/MoviePilot/raw/main/config/app.env) 配置文件，修改好配置后放置到配置文件映射路径根目录，配置项可根据说明自主增减。

配置文件映射路径：`/config`，配置项生效优先级：环境变量 > env文件 > 默认值，**部分参数如路径映射、站点认证、权限端口、时区等必须通过环境变量进行配置**。

> ❗号标识的为必填项，其它为可选项，可选项可删除配置变量从而使用默认值。

### 1. **基础设置**

- **❗NGINX_PORT：** WEB服务端口，默认`3000`，可自行修改，不能与API服务端口冲突（仅支持环境变量配置）
- **❗PORT：** API服务端口，默认`3001`，可自行修改，不能与WEB服务端口冲突（仅支持环境变量配置）
- **PUID**：运行程序用户的`uid`，默认`0`（仅支持环境变量配置）
- **PGID**：运行程序用户的`gid`，默认`0`（仅支持环境变量配置）
- **UMASK**：掩码权限，默认`000`，可以考虑设置为`022`（仅支持环境变量配置）
- **PROXY_HOST：** 网络代理，访问themoviedb或者重启更新需要使用代理访问，格式为`http(s)://ip:port`、`socks5://user:pass@host:port`（仅支持环境变量配置）
- **MOVIEPILOT_AUTO_UPDATE：** 重启时自动更新，`true`/`release`/`dev`/`false`，默认`release`，需要能正常连接Github **注意：如果出现网络问题可以配置`PROXY_HOST`**（仅支持环境变量配置）
- **AUTO_UPDATE_RESOURCE**：启动时自动检测和更新资源包（站点索引及认证等），`true`/`false`，默认`true`，需要能正常连接Github，仅支持Docker
- **GITHUB_TOKEN：** Github token，提高自动更新等请求Github Api的限流阈值，格式：ghp_****（仅支持环境变量配置）
---
- **❗SUPERUSER：** 超级管理员用户名，默认`admin`，安装后使用该用户登录后台管理界面
- **❗SUPERUSER_PASSWORD：** 超级管理员初始密码，默认`password`，建议修改为复杂密码
- **❗API_TOKEN：** API密钥，默认`moviepilot`，在媒体服务器Webhook、微信回调等地址配置中需要加上`?token=`该值，建议修改为复杂字符串
---
- **TMDB_API_DOMAIN：** TMDB API地址，默认`api.themoviedb.org`，也可配置为`api.tmdb.org`、`tmdb.movie-pilot.org` 或其它中转代理服务地址，能连通即可
- **TMDB_IMAGE_DOMAIN：** TMDB图片地址，默认`image.tmdb.org`，可配置为其它中转代理以加速TMDB图片显示，如：`static-mdb.v.geilijiasu.com`
- **WALLPAPER：** 登录首页电影海报，`tmdb`/`bing`，默认`tmdb`
- **RECOGNIZE_SOURCE：** 媒体信息识别来源，`themoviedb`/`douban`，默认`themoviedb`，使用`douban`时不支持二级分类
---
- **SCRAP_METADATA：** 刮削入库的媒体文件，`true`/`false`，默认`true`
- **SCRAP_SOURCE：** 刮削元数据及图片使用的数据源，`themoviedb`/`douban`，默认`themoviedb`
- **SCRAP_FOLLOW_TMDB：** 新增已入库媒体是否跟随TMDB信息变化，`true`/`false`，默认`true`，为`false`时即使TMDB信息变化了也会仍然按历史记录中已入库的信息进行刮削
---
- **❗LIBRARY_PATH：** 媒体库目录，多个目录使用`,`分隔
- **LIBRARY_MOVIE_NAME：** 电影媒体库目录名称（不是完整路径），默认`电影`
- **LIBRARY_TV_NAME：** 电视剧媒体库目录称（不是完整路径），默认`电视剧`
- **LIBRARY_ANIME_NAME：** 动漫媒体库目录称（不是完整路径），默认`电视剧/动漫`
- **LIBRARY_CATEGORY：** 媒体库二级分类开关，`true`/`false`，默认`false`，开启后会根据配置 [category.yaml](https://github.com/jxxghp/MoviePilot/raw/main/config/category.yaml) 自动在媒体库目录下建立二级目录分类
- **❗TRANSFER_TYPE：** 整理转移方式，支持`link`/`copy`/`move`/`softlink`/`rclone_copy`/`rclone_move`  **注意：在`link`和`softlink`转移方式下，转移后的文件会继承源文件的权限掩码，不受`UMASK`影响；rclone需要自行映射rclone配置目录到容器中或在容器内完成rclone配置，节点名称必须为：`MP`**
- **OVERWRITE_MODE：** 转移覆盖模式，默认为`size`，支持`nerver`/`size`/`always`/`latest`，分别表示`不覆盖同名文件`/`同名文件根据文件大小覆盖（大覆盖小）`/`总是覆盖同名文件`/`仅保留最新版本，删除旧版本文件（包括非同名文件）`
---
- **❗COOKIECLOUD_HOST：** CookieCloud服务器地址，格式：`http(s)://ip:port`，不配置默认使用内建服务器`https://movie-pilot.org/cookiecloud`
- **❗COOKIECLOUD_KEY：** CookieCloud用户KEY
- **❗COOKIECLOUD_PASSWORD：** CookieCloud端对端加密密码
- **❗COOKIECLOUD_INTERVAL：** CookieCloud同步间隔（分钟）
- **❗USER_AGENT：** CookieCloud保存Cookie对应的浏览器UA，建议配置，设置后可增加连接站点的成功率，同步站点后可以在管理界面中修改
---
- **SUBSCRIBE_MODE：** 订阅模式，`rss`/`spider`，默认`spider`，`rss`模式通过定时刷新RSS来匹配订阅（RSS地址会自动获取，也可手动维护），对站点压力小，同时可设置订阅刷新周期，24小时运行，但订阅和下载通知不能过滤和显示免费，推荐使用rss模式。
- **SUBSCRIBE_RSS_INTERVAL：** RSS订阅模式刷新时间间隔（分钟），默认`30`分钟，不能小于5分钟。
- **SUBSCRIBE_SEARCH：** 订阅搜索，`true`/`false`，默认`false`，开启后会每隔24小时对所有订阅进行全量搜索，以补齐缺失剧集（一般情况下正常订阅即可，订阅搜索只做为兜底，会增加站点压力，不建议开启）。
- **AUTO_DOWNLOAD_USER：** 远程交互搜索时自动择优下载的用户ID（消息通知渠道的用户ID），多个用户使用,分割，未设置需要选择资源或者回复`0`
---
- **OCR_HOST：** OCR识别服务器地址，格式：`http(s)://ip:port`，用于识别站点验证码实现自动登录获取Cookie等，不配置默认使用内建服务器`https://movie-pilot.org`，可使用 [这个镜像](https://hub.docker.com/r/jxxghp/moviepilot-ocr) 自行搭建。
---
- **❗MESSAGER：** 消息通知渠道，支持 `telegram`/`wechat`/`slack`/`synologychat`，开启多个渠道时使用`,`分隔。同时还需要配置对应渠道的环境变量，非对应渠道的变量可删除，推荐使用`telegram`

  - `wechat`设置项：

    - **WECHAT_CORPID：** WeChat企业ID
    - **WECHAT_APP_SECRET：** WeChat应用Secret
    - **WECHAT_APP_ID：** WeChat应用ID
    - **WECHAT_TOKEN：** WeChat消息回调的Token
    - **WECHAT_ENCODING_AESKEY：** WeChat消息回调的EncodingAESKey
    - **WECHAT_ADMINS：** WeChat管理员列表，多个管理员用英文逗号分隔（可选）
    - **WECHAT_PROXY：** WeChat代理服务器（后面不要加/）

  - `telegram`设置项：

    - **TELEGRAM_TOKEN：** Telegram Bot Token
    - **TELEGRAM_CHAT_ID：** Telegram Chat ID
    - **TELEGRAM_USERS：** Telegram 用户ID，多个使用,分隔，只有用户ID在列表中才可以使用Bot，如未设置则均可以使用Bot
    - **TELEGRAM_ADMINS：** Telegram 管理员ID，多个使用,分隔，只有管理员才可以操作Bot菜单，如未设置则均可以操作菜单（可选）

  - `slack`设置项：

    - **SLACK_OAUTH_TOKEN：** Slack Bot User OAuth Token
    - **SLACK_APP_TOKEN：** Slack App-Level Token
    - **SLACK_CHANNEL：** Slack 频道名称，默认`全体`（可选）
  
  - `synologychat`设置项：

    - **SYNOLOGYCHAT_WEBHOOK：** 在Synology Chat中创建机器人，获取机器人`传入URL`
    - **SYNOLOGYCHAT_TOKEN：** SynologyChat机器人`令牌`
---
- **❗DOWNLOAD_PATH：** 下载保存目录，**注意：需要将`moviepilot`及`下载器`的映射路径保持一致**，否则会导致下载文件无法转移
- **DOWNLOAD_MOVIE_PATH：** 电影下载保存目录路径，不设置则下载到`DOWNLOAD_PATH`
- **DOWNLOAD_TV_PATH：** 电视剧下载保存目录路径，不设置则下载到`DOWNLOAD_PATH`
- **DOWNLOAD_ANIME_PATH：** 动漫下载保存目录路径，不设置则下载到`DOWNLOAD_PATH`
- **DOWNLOAD_CATEGORY：** 下载二级分类开关，`true`/`false`，默认`false`，开启后会根据配置 [category.yaml](https://github.com/jxxghp/MoviePilot/raw/main/config/category.yaml) 自动在下载目录下建立二级目录分类
- **DOWNLOAD_SUBTITLE：** 下载站点字幕，`true`/`false`，默认`true`
---
- **❗DOWNLOADER：** 下载器，支持`qbittorrent`/`transmission`，QB版本号要求>= 4.3.9，TR版本号要求>= 3.0，同时还需要配置对应渠道的环境变量，非对应渠道的变量可删除，推荐使用`qbittorrent`

  - `qbittorrent`设置项：

    - **QB_HOST：** qbittorrent地址，格式：`ip:port`，https需要添加`https://`前缀
    - **QB_USER：** qbittorrent用户名
    - **QB_PASSWORD：** qbittorrent密码
    - **QB_CATEGORY：** qbittorrent分类自动管理，`true`/`false`，默认`false`，开启后会将下载二级分类传递到下载器，由下载器管理下载目录，需要同步开启`DOWNLOAD_CATEGORY`
    - **QB_SEQUENTIAL：** qbittorrent按顺序下载，`true`/`false`，默认`true`
    - **QB_FORCE_RESUME：** qbittorrent忽略队列限制，强制继续，`true`/`false`，默认 `false`

  - `transmission`设置项：

    - **TR_HOST：** transmission地址，格式：`ip:port`，https需要添加`https://`前缀
    - **TR_USER：** transmission用户名
    - **TR_PASSWORD：** transmission密码
- **DOWNLOADER_MONITOR：** 下载器监控，`true`/`false`，默认为`true`，开启后下载完成时才会自动整理入库
- **TORRENT_TAG：** 下载器种子标签，默认为`MOVIEPILOT`，设置后只有MoviePilot添加的下载才会处理，留空所有下载器中的任务均会处理
---
- **❗MEDIASERVER：** 媒体服务器，支持`emby`/`jellyfin`/`plex`，同时开启多个使用`,`分隔。还需要配置对应媒体服务器的环境变量，非对应媒体服务器的变量可删除，推荐使用`emby`

  - `emby`设置项：

    - **EMBY_HOST：** Emby服务器地址，格式：`ip:port`，https需要添加`https://`前缀
    - **EMBY_API_KEY：** Emby Api Key，在`设置->高级->API密钥`处生成

  - `jellyfin`设置项：

    - **JELLYFIN_HOST：** Jellyfin服务器地址，格式：`ip:port`，https需要添加`https://`前缀
    - **JELLYFIN_API_KEY：** Jellyfin Api Key，在`设置->高级->API密钥`处生成

  - `plex`设置项：

    - **PLEX_HOST：** Plex服务器地址，格式：`ip:port`，https需要添加`https://`前缀
    - **PLEX_TOKEN：** Plex网页Url中的`X-Plex-Token`，通过浏览器F12->网络从请求URL中获取
- **MEDIASERVER_SYNC_INTERVAL:** 媒体服务器同步间隔（小时），默认`6`，留空则不同步
- **MEDIASERVER_SYNC_BLACKLIST:** 媒体服务器同步黑名单，多个媒体库名称使用,分割


### 2. **用户认证**

`MoviePilot`需要认证后才能使用，配置`AUTH_SITE`后，需要根据下表配置对应站点的认证参数（**仅能通过环境变量配置**）

`AUTH_SITE`支持配置多个认证站点，使用`,`分隔，如：`iyuu,hhclub`，会依次执行认证操作，直到有一个站点认证成功。

- **❗AUTH_SITE：** 认证站点，认证资源`v1.0.2`支持`iyuu`/`hhclub`/`audiences`/`hddolby`/`zmpt`/`freefarm`/`hdfans`/`wintersakura`/`leaves`/`1ptba`/`icc2022`/`ptlsp`/`xingtan`/`ptvicomo`/`agsvpt`

|      站点      |                          参数                           |
|:------------:|:-----------------------------------------------------:|
|     iyuu     |                 `IYUU_SIGN`：IYUU登录令牌                  |
|    hhclub    |     `HHCLUB_USERNAME`：用户名<br/>`HHCLUB_PASSKEY`：密钥     |
|  audiences   |    `AUDIENCES_UID`：用户ID<br/>`AUDIENCES_PASSKEY`：密钥    |
|   hddolby    |      `HDDOLBY_ID`：用户ID<br/>`HDDOLBY_PASSKEY`：密钥       |
|     zmpt     |         `ZMPT_UID`：用户ID<br/>`ZMPT_PASSKEY`：密钥         |
|   freefarm   |     `FREEFARM_UID`：用户ID<br/>`FREEFARM_PASSKEY`：密钥     |
|    hdfans    |       `HDFANS_UID`：用户ID<br/>`HDFANS_PASSKEY`：密钥       |
| wintersakura | `WINTERSAKURA_UID`：用户ID<br/>`WINTERSAKURA_PASSKEY`：密钥 |
|    leaves    |       `LEAVES_UID`：用户ID<br/>`LEAVES_PASSKEY`：密钥       |
|    1ptba     |        `1PTBA_UID`：用户ID<br/>`1PTBA_PASSKEY`：密钥        |
|   icc2022    |      `ICC2022_UID`：用户ID<br/>`ICC2022_PASSKEY`：密钥      |
|    ptlsp     |        `PTLSP_UID`：用户ID<br/>`PTLSP_PASSKEY`：密钥        |
|   xingtan    |      `XINGTAN_UID`：用户ID<br/>`XINGTAN_PASSKEY`：密钥      |
|   ptvicomo   |     `PTVICOMO_UID`：用户ID<br/>`PTVICOMO_PASSKEY`：密钥     |
|    agsvpt    |      `AGSVPT_UID`：用户ID<br/>`AGSVPT_PASSKEY`：密钥       |


### 2. **进阶配置**

- **BIG_MEMORY_MODE：** 大内存模式，默认为`false`，开启后会增加缓存数量，占用更多的内存，但响应速度会更快

- **MOVIE_RENAME_FORMAT：** 电影重命名格式

`MOVIE_RENAME_FORMAT`支持的配置项：

> `title`： 标题  
> `original_name`： 原文件名  
> `original_title`： 原语种标题  
> `name`： 识别名称  
> `year`： 年份  
> `resourceType`：资源类型  
> `effect`：特效  
> `edition`： 版本（资源类型+特效）  
> `videoFormat`： 分辨率  
> `releaseGroup`： 制作组/字幕组  
> `customization`： 自定义占位符  
> `videoCodec`： 视频编码  
> `audioCodec`： 音频编码  
> `tmdbid`： TMDBID  
> `imdbid`： IMDBID  
> `part`：段/节  
> `fileExt`：文件扩展名
> `tmdbid`：TMDB ID
> `imdbid`：IMDB ID
> `customization`：自定义占位符

`MOVIE_RENAME_FORMAT`默认配置格式：

```
{{title}}{% if year %} ({{year}}){% endif %}/{{title}}{% if year %} ({{year}}){% endif %}{% if part %}-{{part}}{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}{{fileExt}}
```

- **TV_RENAME_FORMAT：** 电视剧重命名格式

`TV_RENAME_FORMAT`额外支持的配置项：

> `season`： 季号  
> `episode`： 集号  
> `season_episode`： 季集 SxxExx  
> `episode_title`： 集标题

`TV_RENAME_FORMAT`默认配置格式：

```
{{title}}{% if year %} ({{year}}){% endif %}/Season {{season}}/{{title}} - {{season_episode}}{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif %}{{fileExt}}
```


### 3. **优先级规则**

- 仅支持使用内置规则进行排列组合，内置规则有：`蓝光原盘`、`4K`、`1080P`、`中文字幕`、`特效字幕`、`H265`、`H264`、`杜比`、`HDR`、`REMUX`、`WEB-DL`、`免费`、`国语配音` 等
- 符合任一层级规则的资源将被标识选中，匹配成功的层级做为该资源的优先级，排越前面优先级超高
- 不符合过滤规则所有层级规则的资源将不会被选中

### 4. **插件扩展**

- **PLUGIN_MARKET：** 插件市场仓库地址，仅支持Github仓库`main`分支，多个地址使用`,`分隔，默认为官方插件仓库：`https://github.com/jxxghp/MoviePilot-Plugins` ，通过查看[MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)项目的fork，或者查看频道置顶了解更多第三方插件仓库。


## 使用

- 通过CookieCloud同步快速同步站点，不需要使用的站点可在WEB管理界面中禁用，无法同步的站点可手动新增。
- 通过WEB进行管理，将WEB添加到手机桌面获得类App使用效果，管理界面端口：`3000`，后台API端口：`3001`。
- 通过下载器监控或使用目录监控插件实现自动整理入库刮削（二选一）。
- 通过微信/Telegram/Slack/SynologyChat远程管理，其中微信/Telegram将会自动添加操作菜单（微信菜单条数有限制，部分菜单不显示）；微信需要在官方页面设置回调地址，SynologyChat需要设置机器人传入地址，地址相对路径为：`/api/v1/message/`。
- 设置媒体服务器Webhook，通过MoviePilot发送播放通知等。Webhook回调相对路径为`/api/v1/webhook?token=moviepilot`（`3001`端口），其中`moviepilot`为设置的`API_TOKEN`。
- 将MoviePilot做为Radarr或Sonarr服务器添加到Overseerr或Jellyseerr（`API服务端口`），可使用Overseerr/Jellyseerr浏览订阅。
- 映射宿主机docker.sock文件到容器`/var/run/docker.sock`，以支持内建重启操作。实例：`-v /var/run/docker.sock:/var/run/docker.sock:ro`

### **注意**
- 容器首次启动需要下载浏览器内核，根据网络情况可能需要较长时间，此时无法登录。可映射`/moviepilot`目录避免容器重置后重新触发浏览器内核下载。 
- 使用反向代理时，需要添加以下配置，否则可能会导致部分功能无法访问（`ip:port`修改为实际值）：
```nginx configuration
location / {
    proxy_pass http://ip:port;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```
- 新建的企业微信应用需要固定公网IP的代理才能收到消息，代理添加以下代码：
```nginx configuration
location /cgi-bin/gettoken {
    proxy_pass https://qyapi.weixin.qq.com;
}
location /cgi-bin/message/send {
    proxy_pass https://qyapi.weixin.qq.com;
}
location  /cgi-bin/menu/create {
    proxy_pass https://qyapi.weixin.qq.com;
}
```

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/f2654b09-26f3-464f-a0af-1de3f97832ee)

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/fcb87529-56dd-43df-8337-6e34b8582819)

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/bfa77c71-510a-46a6-9c1e-cf98cb101e3a)

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/51cafd09-e38c-47f9-ae62-1e83ab8bf89b)

