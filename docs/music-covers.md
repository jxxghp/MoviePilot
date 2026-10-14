# 音乐本地封面

开启整理请求的“同步整理附加文件”后，音乐目录或单个音轨整理会携带同目录的封面图片。
图片沿用当前复制、硬链接、移动和覆盖策略；重命名时保留图片原名，放入目标专辑根目录，
不套用曲名、曲序或 Disc 子目录。专辑、EP 和单曲共用这一行为。

本地图片作为附件保留，不依赖在线封面是否可下载，也不要求开启刮削。
同目录包含多个不同发行时不自动分配封面；影视整理和单独选择图片不会触发音乐封面整理。
后续生成 STRM 时仍需让对应插件同步图片后缀，MoviePilot 不负责该插件的复制配置。

音乐刮削内嵌封面使用“在线优先，下载不到再读取同目录图片”的顺序；本地候选按配置顺序选择，
损坏或不支持的图片跳过并尝试下一张。目录读取和图片下载均走存储能力，支持本地和远端目标。
刮削为“跳过”时不写内嵌封面，“缺失时刮削”保留已有内嵌封面，“总是刮削”可替换已有封面。
写入继续遵守音频源保护，不通过媒体库硬链接或软链接改写下载源文件。

`MUSIC_COVER_FILE_PATTERNS` 为全局封面匹配配置，逗号分隔、忽略大小写，支持 `*` 等文件名通配符。
默认值为：

```text
cover.jpg,cover.jpeg,cover.png,folder.jpg,folder.jpeg,folder.png,front.jpg,front.jpeg,front.png,*.png
```

匹配始终限制为 JPG、JPEG、PNG、WebP 图片，不读取隐藏文件。可以加入 `*.jpg` 或 `artwork.webp`；
留空关闭本地封面匹配。配置示例：

```shell
moviepilot config set MUSIC_COVER_FILE_PATTERNS 'cover.jpg,folder.jpg,front.jpg,*.png,artwork.webp'
```
