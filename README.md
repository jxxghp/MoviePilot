# MoviePilot

基于 [NAStool](https://github.com/NAStool/nas-tools) 部分代码重新设计，聚焦自动化核心需求，减少问题同时更易于扩展和维护。

# 仅用于学习交流使用，请勿在任何国内平台宣传该项目！

发布频道：https://t.me/moviepilot_channel

## 主要特性
- 前后端分离，基于FastApi + Vue3，前端项目地址：[MoviePilot-Frontend](https://github.com/jxxghp/MoviePilot-Frontend)，API：http://localhost:3001/docs
- 聚焦核心需求，简化功能和设置，部分设置项可直接使用默认值。
- 重新设计了用户界面，更加美观易用。

## 安装

### 注意：管理员用户不要使用弱密码！如非必要不要暴露到公网。如被盗取管理账号权限，将会导致站点Cookie等敏感数据泄露！

### 1. **安装CookieCloud插件**

站点信息需要通过CookieCloud同步获取，因此需要安装CookieCloud插件，将浏览器中的站点Cookie数据同步到云端后再同步到MoviePilot使用。 插件下载地址请点击 [这里](https://github.com/easychen/CookieCloud/releases)。

### 2. **安装CookieCloud服务端（可选）**

通过CookieCloud可以快速同步浏览器中保存的站点数据到MoviePilot，支持以下服务方式：

- 使用公共CookieCloud远程服务器（默认）：服务器地址为：https://movie-pilot.org/cookiecloud
- 使用内建的本地Cookie服务：在 `设定` - `站点` 中打开`启用本地CookieCloud服务器`后，将启用内建的CookieCloud提供服务，服务地址为：`http://localhost:${NGINX_PORT}/cookiecloud/`, Cookie数据加密保存在配置文件目录下的`cookies`文件中
- 自建服务CookieCloud服务器：参考 [CookieCloud](https://github.com/easychen/CookieCloud) 项目进行搭建，docker镜像请点击 [这里](https://hub.docker.com/r/easychen/cookiecloud)

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

  1. 独立执行文件版本：下载 [MoviePilot.exe](https://github.com/jxxghp/MoviePilot/releases)，双击运行后自动生成配置文件目录，访问：http://localhost:3000
  2. 安装包版本：[Windows-MoviePilot](https://github.com/developer-wlj/Windows-MoviePilot)

- 群晖套件

  添加套件源：https://spk7.imnks.com/

- 本地运行

  1) 将工程 [MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins) plugins目录下的所有文件复制到`app/plugins`目录
  2) 将工程 [MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources) resources目录下的所有文件复制到`app/helper`目录
  3) 执行命令：`pip install -r requirements.txt` 安装依赖
  4) 执行命令：`PYTHONPATH=. python app/main.py` 启动服务
  5) 根据前端项目 [MoviePilot-Frontend](https://github.com/jxxghp/MoviePilot-Frontend) 说明，启动前端服务

## 配置

大部分配置可启动后通过WEB管理界面进行配置，但仍有部分配置需要通过环境变量/配置文件进行配置。

配置文件映射路径：`/config`，配置项生效优先级：环境变量 > env文件（或通过WEB界面配置） > 默认值。

> ❗号标识的为必填项，其它为可选项，可选项可删除配置变量从而使用默认值。

### 1. **环境变量**

- **❗NGINX_PORT：** WEB服务端口，默认`3000`，可自行修改，不能与API服务端口冲突
- **❗PORT：** API服务端口，默认`3001`，可自行修改，不能与WEB服务端口冲突
- **PUID**：运行程序用户的`uid`，默认`0`
- **PGID**：运行程序用户的`gid`，默认`0`
- **UMASK**：掩码权限，默认`000`，可以考虑设置为`022`
- **PROXY_HOST：** 网络代理，访问themoviedb或者重启更新需要使用代理访问，格式为`http(s)://ip:port`、`socks5://user:pass@host:port`
- **MOVIEPILOT_AUTO_UPDATE：** 重启时自动更新，`true`/`release`/`dev`/`false`，默认`release`，需要能正常连接Github **注意：如果出现网络问题可以配置`PROXY_HOST`**
- **❗AUTH_SITE：** 认证站点（认证通过后才能使用站点相关功能），支持配置多个认证站点，使用`,`分隔，如：`iyuu,hhclub`，会依次执行认证操作，直到有一个站点认证成功。  

    配置`AUTH_SITE`后，需要根据下表配置对应站点的认证参数。
    认证资源`v1.2.4+`支持：`iyuu`/`hhclub`/`audiences`/`hddolby`/`zmpt`/`freefarm`/`hdfans`/`wintersakura`/`leaves`/`ptba` /`icc2022`/`ptlsp`/`xingtan`/`ptvicomo`/`agsvpt`/`hdkyl`/`qingwa`
  
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
    |     ptba     |         `PTBA_UID`：用户ID<br/>`PTBA_PASSKEY`：密钥         |
    |   icc2022    |      `ICC2022_UID`：用户ID<br/>`ICC2022_PASSKEY`：密钥      |
    |    ptlsp     |        `PTLSP_UID`：用户ID<br/>`PTLSP_PASSKEY`：密钥        |
    |   xingtan    |      `XINGTAN_UID`：用户ID<br/>`XINGTAN_PASSKEY`：密钥      |
    |   ptvicomo   |     `PTVICOMO_UID`：用户ID<br/>`PTVICOMO_PASSKEY`：密钥     |
    |    agsvpt    |       `AGSVPT_UID`：用户ID<br/>`AGSVPT_PASSKEY`：密钥       |
    |    hdkyl     |        `HDKYL_UID`：用户ID<br/>`HDKYL_PASSKEY`：密钥        |
    |   qingwa    |      `QINGWA_UID`：用户ID<br/>`QINGWA_PASSKEY`：密钥      |


### 2. **环境变量 / 配置文件**

配置文件名：`app.env`，放配置文件根目录。

- **❗SUPERUSER：** 超级管理员用户名，默认`admin`，安装后使用该用户登录后台管理界面，**注意：启动一次后再次修改该值不会生效，除非删除数据库文件！**
- **❗API_TOKEN：** API密钥，默认`moviepilot`，在媒体服务器Webhook、微信回调等地址配置中需要加上`?token=`该值，建议修改为复杂字符串
- **BIG_MEMORY_MODE：** 大内存模式，默认为`false`，开启后会增加缓存数量，占用更多的内存，但响应速度会更快
- **DOH_ENABLE：** DNS over HTTPS开关，`true`/`false`，默认`true`，开启后会使用DOH对api.themoviedb.org等域名进行解析，以减少被DNS污染的情况，提升网络连通性
- **META_CACHE_EXPIRE：** 元数据识别缓存过期时间（小时），数字型，不配置或者配置为0时使用系统默认（大内存模式为7天，否则为3天），调大该值可减少themoviedb的访问次数
- **GITHUB_TOKEN：** Github token，提高自动更新、插件安装等请求Github Api的限流阈值，格式：ghp_****
- **DEV:** 开发者模式，`true`/`false`，默认`false`，开启后会暂停所有定时任务
- **AUTO_UPDATE_RESOURCE**：启动时自动检测和更新资源包（站点索引及认证等），`true`/`false`，默认`true`，需要能正常连接Github，仅支持Docker镜像
---
- **TMDB_API_DOMAIN：** TMDB API地址，默认`api.themoviedb.org`，也可配置为`api.tmdb.org`、`tmdb.movie-pilot.org` 或其它中转代理服务地址，能连通即可
- **TMDB_IMAGE_DOMAIN：** TMDB图片地址，默认`image.tmdb.org`，可配置为其它中转代理以加速TMDB图片显示，如：`static-mdb.v.geilijiasu.com`
- **WALLPAPER：** 登录首页电影海报，`tmdb`/`bing`，默认`tmdb`
- **RECOGNIZE_SOURCE：** 媒体信息识别来源，`themoviedb`/`douban`，默认`themoviedb`，使用`douban`时不支持二级分类，且受豆瓣控流限制
- **FANART_ENABLE：** Fanart开关，`true`/`false`，默认`true`，关闭后刮削的图片类型会大幅减少
- **SCRAP_SOURCE：** 刮削元数据及图片使用的数据源，`themoviedb`/`douban`，默认`themoviedb`
- **SCRAP_FOLLOW_TMDB：** 新增已入库媒体是否跟随TMDB信息变化，`true`/`false`，默认`true`，为`false`时即使TMDB信息变化了也会仍然按历史记录中已入库的信息进行刮削
---
- **AUTO_DOWNLOAD_USER：** 远程交互搜索时自动择优下载的用户ID（消息通知渠道的用户ID），多个用户使用,分割，设置为 all 代表全部用户自动择优下载，未设置需要手动选择资源或者回复`0`才自动择优下载
---
- **OCR_HOST：** OCR识别服务器地址，格式：`http(s)://ip:port`，用于识别站点验证码实现自动登录获取Cookie等，不配置默认使用内建服务器`https://movie-pilot.org`，可使用 [这个镜像](https://hub.docker.com/r/jxxghp/moviepilot-ocr) 自行搭建。
---
- **DOWNLOAD_SUBTITLE：** 下载站点字幕，`true`/`false`，默认`true`
---
- **SEARCH_MULTIPLE_NAME：** 搜索时是否使用多个名称搜索，`true`/`false`，默认`false`，开启后会使用多个名称进行搜索，搜索结果会更全面，但会增加搜索时间；关闭时只要其中一个名称搜索到结果或全部名称搜索完毕即停止
---
- **MOVIE_RENAME_FORMAT：** 电影重命名格式，基于jinjia2语法

  `MOVIE_RENAME_FORMAT`支持的配置项：

  > `title`： TMDB/豆瓣中的标题  
  > `en_title`： TMDB中的英文标题 （暂不支持豆瓣）
  > `original_title`： TMDB/豆瓣中的原语种标题  
  > `name`： 从文件名中识别的名称（同时存在中英文时，优先使用中文）
  > `en_name`：从文件名中识别的英文名称（可能为空）
  > `original_name`： 原文件名（包括文件外缀）  
  > `year`： 年份  
  > `resourceType`：资源类型  
  > `effect`：特效  
  > `edition`： 版本（资源类型+特效）  
  > `videoFormat`： 分辨率  
  > `releaseGroup`： 制作组/字幕组  
  > `customization`： 自定义占位符  
  > `videoCodec`： 视频编码  
  > `audioCodec`： 音频编码  
  > `tmdbid`： TMDB ID（非TMDB识别源时为空）  
  > `imdbid`： IMDB ID（可能为空）  
  > `doubanid`：豆瓣ID（非豆瓣识别源时为空）  
  > `part`：段/节  
  > `fileExt`：文件扩展名
  > `customization`：自定义占位符
  
  `MOVIE_RENAME_FORMAT`默认配置格式：
  
  ```
  {{title}}{% if year %} ({{year}}){% endif %}/{{title}}{% if year %} ({{year}}){% endif %}{% if part %}-{{part}}{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}{{fileExt}}
  ```

- **TV_RENAME_FORMAT：** 电视剧重命名格式，基于jinjia2语法

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

- 仅支持使用内置规则进行排列组合，通过设置多层规则来实现优先级顺序匹配
- 符合任一层级规则的资源将被标识选中，匹配成功的层级做为该资源的优先级，排越前面优先级超高
- 不符合过滤规则所有层级规则的资源将不会被选中

### 4. **插件扩展**

- **PLUGIN_MARKET：** 插件市场仓库地址，仅支持Github仓库`main`分支，多个地址使用`,`分隔，默认为官方插件仓库：`https://github.com/jxxghp/MoviePilot-Plugins` ，通过查看[MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)项目的fork，或者查看频道置顶了解更多第三方插件仓库。


## 使用

### 1. **WEB后台管理**
- 通过设置的超级管理员用户登录后台管理界面（`SUPERUSER`配置项，默认用户：admin，默认端口：3000）
> ❗**注意：超级管理员用户初始密码为自动生成，需要在首次运行时的后台日志中查看！** 如首次运行日志丢失，则需要删除配置文件目录下的`user.db`文件，然后重启服务。
### 2. **站点维护**
- 通过CookieCloud同步快速添加站点，不需要使用的站点可在WEB管理界面中禁用或删除，无法同步的站点也可手动新增。
- 需要通过环境变量设置用户认证信息且认证成功后才能使用站点相关功能，未认证通过时站点相关的插件也会无法显示。
### 3. **文件整理**
- 默认通过监控下载器实现下载完成后自动整理入库并刮削媒体信息，需要后台打开`下载器监控`开关，且仅会处理通过MoviePilot添加下载的任务。
- 下载器监控默认轮循间隔为5分钟，如果是使用qbittorrent，可在 `QB设置`->`下载完成时运行外部程序` 处填入：`curl "http://localhost:3000/api/v1/transfer/now?token=moviepilot" `，实现无需等待轮循下载完成后立即整理入库（地址、端口和token按实际调整，curl也可更换为wget）。
- 使用`目录监控`等插件实现更灵活的自动整理。
### 4. **通知交互**
- 支持通过`微信`/`Telegram`/`Slack`/`SynologyChat`/`VoceChat`等渠道远程管理和订阅下载，其中 微信/Telegram 将会自动添加操作菜单（微信菜单条数有限制，部分菜单不显示）。
- `微信`回调地址、`SynologyChat`传入地址地址相对路径均为：`/api/v1/message/`；`VoceChat`的Webhook地址相对路径为：`/api/v1/message/?token=moviepilot`，其中moviepilot为设置的`API_TOKEN`。
### 5. **订阅与搜索**
- 通过MoviePilot管理后台搜索和订阅。
- 将MoviePilot做为`Radarr`或`Sonarr`服务器添加到`Overseerr`或`Jellyseerr`，可使用`Overseerr/Jellyseerr`浏览和添加订阅。
- 安装`豆瓣榜单订阅`、`猫眼订阅`等插件，实现自动订阅豆瓣榜单、猫眼榜单等。
### 6. **其他**
- 通过设置媒体服务器Webhook指向MoviePilot（相对路径为`/api/v1/webhook?token=moviepilot`，其中`moviepilot`为设置的`API_TOKEN`），可实现通过MoviePilot发送播放通知，以及配合各类插件实现播放限速等功能。
- 映射宿主机`docker.sock`文件到容器`/var/run/docker.sock`，可支持应用内建重启操作。实例：`-v /var/run/docker.sock:/var/run/docker.sock:ro`。
- 将WEB页面添加到手机桌面图标可获得与App一样的使用体验。

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
- 反代使用ssl时，需要开启`http2`，否则会导致日志加载时间过长或不可用。以`Nginx`为例：
```nginx configuration
server {
    listen 443 ssl;
    http2 on;
    # ...
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

- 部分插件功能基于文件系统监控实现（如`目录监控`等），需在宿主机上（不是docker容器内）执行以下命令并重启：
```shell
echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/f2654b09-26f3-464f-a0af-1de3f97832ee)

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/fcb87529-56dd-43df-8337-6e34b8582819)

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/bfa77c71-510a-46a6-9c1e-cf98cb101e3a)

![image](https://github.com/jxxghp/MoviePilot/assets/51039935/51cafd09-e38c-47f9-ae62-1e83ab8bf89b)

