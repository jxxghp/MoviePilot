# 媒体服务器查询内容契约

本次实现范围为 Emby、Jellyfin、Plex、Navidrome。极空间、飞牛、绿联和 Vyo 按用户要求暂不修改。
调用方通过 `MediaServerQueryOptions` 的 `fields` 选择需要的内容，服务适配器负责翻译为已确认的服务端参数。
`query=None` 或 `fields=None` 保留每个接口原有的请求和默认返回内容；显式空集合只请求完成该操作必要的基础信息。
条目 ID、类型、展示名称以及完成查询、权限校验、身份匹配所必需的字段不因裁剪丢失。

| 内容组 | 用途 |
| --- | --- |
| library | 条目所属库或父目录 |
| original_title | 原始标题 |
| year | 年份和用于展示的日期信息 |
| identity | 媒体来源与来源内 ID，成对保留 |
| path | 文件、目录和媒体版本路径 |
| music | 艺术家、专辑与音乐匹配所需的附加信息 |
| user_state | 播放状态、播放次数、进度和最后播放时间 |
| images | 图片元数据、图片链接及附加图片查询 |
| counts | 媒体库数量及非业务必需的总量查询 |
| link | 播放/媒体库跳转链接 |

链和模块使用同一个可选 `query` 参数。它是模块契约显式登记的兼容参数，旧插件 provider 没有声明时由既有调度兼容机制省略；不得吞掉其它未知参数。
同一查询的递归、分页和补查必须保留选项，缓存必须区分不同投影，避免精简结果污染默认结果。

Emby/Jellyfin 的 `Fields` 是追加字段；关闭图片和用户数据必须分别使用服务端开关。
Plex 应区分属性与子节点，保留业务需要的 Media/Guid，且不能混淆旧 API 与 API 1.0+ 的 includeFields 语义。
其他服务只使用有证据的参数，减少不必要的图片、统计与详情请求；不把本地丢弃响应字段描述成服务端裁剪。
服务端没有已确认字段选择协议时，其默认返回的基础负载仍可能包含额外属性，该限制应逐服务记录。

验证必须覆盖：默认请求及结果兼容、显式投影的线上请求参数、递归/分页/补查透传、旧 provider 兼容，以及同步、查重、播放、首页和壁纸等调用方的实际字段需求。

## 调用示例

```python
from app.sdk.services import MediaServerField, MediaServerQueryOptions

# 只请求同步所需的来源与路径，不取图片和播放状态。
query = MediaServerQueryOptions(fields={MediaServerField.IDENTITY, MediaServerField.PATH})
items = media_chain.items(server="main", library_id="movies", query=query)
```

## 主程序调用需求

| 调用方 | 内容需求 |
| --- | --- |
| 同步库目录 | 路径及进度需要的数量；先应用同步库范围，再统计；复用目录已有数量 |
| 同步条目 | 所属库、原始标题、年份、来源身份、路径和音乐附加信息；不请求播放状态和图片 |
| 同步分集、订阅分集跳转 | 条目 ID 与季集号等基础信息；身份校验另保留必要来源字段 |
| 下载、订阅、工作流、Servarr 与音乐查重 | 来源身份与年份；音乐匹配补充专辑与曲目完整性字段 |
| 播放入口 | 基础条目和服务器标识，用于生成播放地址 |
| 最新入库卡片 | 年份、图片及跳转链接 |
| 继续观看卡片 | 年份、图片、跳转链接及用户进度 |
| 媒体库卡片 | 路径、图片、数量及跳转链接 |
| 壁纸 | 图片；不构造不用的播放链接和播放状态 |
| Agent 原生查询 | 动作参数 `query` 显式选择；省略时保留原有完整结果 |

主要入口与调用位置：

| 模块 | 调用用途 |
| --- | --- |
| [媒体服务器 API](../app/api/endpoints/mediaserver.py) | 播放入口、查重、最新、继续观看、库卡片 |
| [媒体服务器链](../app/chain/mediaserver.py) | 统一读取门面、全量同步、分集同步及壁纸 |
| [下载查重](../app/chain/download/existence.py)、[订阅查询](../app/chain/subscribe/query.py) | 入库判断、已有季集及跳转条目标识 |
| [Servarr API](../app/api/servarr.py)、[音乐 API](../app/api/endpoints/music.py) | Radarr/Sonarr 兼容查询和音乐入库状态 |
| [下载工作流](../app/workflow/actions/add_download.py) | 仅补缺模式下的入库判断 |
| [启动装配](../app/startup/composition/chain.py) | 向壁纸服务注入仅需图片的查询回调 |
| [Agent 原生动作](../skills/mediaserver-operation/scripts/mp-mediaserver.py)、[工具 Schema](../app/agent/tools/impl/service.py) | 校验与透传查询内容，并从同一模型推导工具输入合同 |

## 已确认的协议映射与限制

| 服务 | 远端或附加请求控制 |
| --- | --- |
| Emby/Jellyfin | 按所需组追加 Fields，独立控制 EnableImages/EnableUserData；精简详情改为列表按 Ids 查询；递归保留投影 |
| Plex | 使用 excludeFields/excludeElements，避免 includeFields 的跨版本语义变化；按需保留 Media 与 Guid，禁止缺失字段触发详情重载；库目录可跳过封面候选与统计 |
| 极空间、飞牛、绿联、Vyo | 按用户要求暂不修改适配器；模块调度保留旧签名兼容，Agent 原生动作暂不接受 query |
| Navidrome | Subsonic 返回图片标识而非图片内容；精简调用直接使用 size/offset 分页，不先枚举全库再切片；目录可跳过全量计数 |

上述限制意味着部分服务仍可能在基础响应中返回调用方未使用的属性；完整厂商 schema 和具体版本的能力验证仍需服务端证据。用户请求的投影不会削弱媒体身份匹配、同步范围或失败保护。
