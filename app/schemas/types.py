import re
from enum import Enum
from typing import Literal, Optional, Tuple, Union

from pydantic import GetJsonSchemaHandler
from pydantic_core import CoreSchema


# 音乐实体命名空间由公共类型模块统一持有，避免模型、接口和工具层重复定义。
MUSIC_ENTITY_RECORDING = "recording"
MUSIC_ENTITY_ALBUM = "album"
MUSIC_ENTITY_ARTIST = "artist"
MusicEntityType = Literal["recording", "album", "artist"]
MusicTargetEntityType = Literal["recording", "album"]
MUSIC_ENTITY_TYPES = frozenset({
    MUSIC_ENTITY_RECORDING,
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_ARTIST,
})
MUSIC_SUBSCRIBABLE_TYPES = frozenset({
    MUSIC_ENTITY_RECORDING,
    MUSIC_ENTITY_ALBUM,
})


class ReplyMode(str, Enum):
    """Agent 最终回复的投递策略，供编排层与调用层共享。"""

    DISPATCH = "dispatch"
    CAPTURE_ONLY = "capture_only"

# ListenBrainz 音乐探索能力的参数取值域契约，供入口层校验、链层与模块实现共用
# ListenBrainz 全站统计支持的周期，取值与官方统计页面完全一致
LISTENBRAINZ_CHART_RANGES = (
    "this_week",
    "this_month",
    "this_year",
    "week",
    "month",
    "quarter",
    "half_yearly",
    "year",
    "all_time",
)
# ListenBrainz 官方新发行页面支持的排序方式
LISTENBRAINZ_FRESH_SORTS = (
    "release_date",
    "artist_credit_name",
    "release_name",
)
# ListenBrainz 新发行页面允许回溯或预告的最大天数
LISTENBRAINZ_FRESH_MAX_DAYS = 90


# 媒体类型
class MediaType(Enum):
    MOVIE = '电影'
    TV = '电视剧'
    MUSIC = '音乐'
    COLLECTION = '系列'
    UNKNOWN = '未知'

    @staticmethod
    def from_agent(key: str) -> Optional["MediaType"]:
        """将 Agent 媒体类型转换为 MediaType。"""
        _map = {
            "movie": MediaType.MOVIE,
            "tv": MediaType.TV,
            "music": MediaType.MUSIC,
        }
        return _map.get(key.strip().lower() if key else "")

    def to_agent(self) -> str:
        """将 MediaType 转换为 Agent 使用的媒体类型。"""
        return {
            MediaType.MOVIE: "movie",
            MediaType.TV: "tv",
            MediaType.MUSIC: "music",
        }.get(self, self.value)


MEDIA_SOURCE_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9._-]{0,63}$"
_MEDIA_SOURCE_IDENTIFIER_RE = re.compile(MEDIA_SOURCE_IDENTIFIER_PATTERN)
_MEDIA_SOURCE_VALUE_ALIASES = {
    "tmdb": "themoviedb",
    "audio_db": "theaudiodb",
    "douban_music": "doubanmusic",
    "mango_tv": "mangguodiscover",
    "migu_video": "migu",
    "tencent_video": "tencentvideodiscover",
    "iqiyi": "iqiyidiscover",
}


class MediaSource(str, Enum):
    """媒体主身份的数据来源，内置来源为常量，插件来源为动态扩展成员。"""

    TMDB = "themoviedb"
    Douban = "douban"
    Bangumi = "bangumi"
    AniList = "anilist"
    IMDb = "imdb"
    TVDB = "tvdb"
    MusicBrainz = "musicbrainz"
    TheAudioDB = "theaudiodb"
    DoubanMusic = "doubanmusic"
    Bilibili = "bilibili"
    MangoTV = "mangguodiscover"
    MiguVideo = "migu"
    TencentVideo = "tencentvideodiscover"
    Iqiyi = "iqiyidiscover"

    def __str__(self) -> str:
        """返回可直接用于 API 和数据库的规范值。"""
        return self.value

    @classmethod
    def _missing_(cls, value: object) -> Optional["MediaSource"]:
        """将合法插件来源标识解析为动态枚举成员，并规范化内置别名。"""
        if not isinstance(value, str):
            return None
        normalized = value.strip().casefold()
        normalized = _MEDIA_SOURCE_VALUE_ALIASES.get(normalized, normalized)
        known_member = cls._value2member_map_.get(normalized)
        if known_member:
            return known_member
        if not _MEDIA_SOURCE_IDENTIFIER_RE.fullmatch(normalized):
            return None
        member = str.__new__(cls, normalized)
        member._name_ = normalized
        member._value_ = normalized
        cls._value2member_map_.setdefault(normalized, member)
        return cls._value2member_map_[normalized]

    @classmethod
    def __get_pydantic_json_schema__(
            cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler,
    ) -> dict:
        """在 OpenAPI 中声明可扩展标识格式，避免把内置成员误写成完整白名单。"""
        schema = handler(core_schema)
        schema.pop("enum", None)
        schema["pattern"] = MEDIA_SOURCE_IDENTIFIER_PATTERN
        schema["examples"] = [source.value for source in cls]
        return schema


# 搜索可以选择一个或多个内置或插件扩展来源。
MediaSourceSelection = Union[MediaSource, Tuple[MediaSource, ...]]


def media_type_to_agent(value) -> Optional[str]:
    """将枚举、Agent 键或数据库枚举值统一转换为 Agent 媒体类型。"""
    if isinstance(value, MediaType):
        return value.to_agent()
    if isinstance(value, str):
        mt = MediaType.from_agent(value)
        if not mt:
            try:
                mt = MediaType(value)
            except ValueError:
                pass
        return mt.to_agent() if mt else value
    return None


# 排序类型枚举
class SortType(Enum):
    TIME = "time"  # 按时间排序
    COUNT = "count"  # 按人数排序
    RATING = "rating"  # 按评分排序


# 种子状态
class TorrentStatus(Enum):
    TRANSFER = "可转移"
    DOWNLOADING = "下载中"


# 下载器任务查询状态
class TorrentQueryStatus(Enum):
    ALL = "all"
    TRANSFER = "transfer"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    PAUSED = "paused"


# 下载器任务归一状态
class DownloadTaskState(Enum):
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"


# 异步广播事件
class EventType(Enum):
    # 插件需要重载
    PluginReload = "plugin.reload"
    # 触发插件动作
    PluginAction = "plugin.action"
    # 插件触发事件
    PluginTriggered = "plugin.triggered"
    # 执行命令
    CommandExcute = "command.excute"
    # 站点已删除
    SiteDeleted = "site.deleted"
    # 站点已更新
    SiteUpdated = "site.updated"
    # 站点已刷新
    SiteRefreshed = "site.refreshed"
    # 媒体文件整理完成
    TransferComplete = "transfer.complete"
    # 媒体文件整理失败
    TransferFailed = "transfer.failed"
    # 字幕整理完成
    SubtitleTransferComplete = "transfer.subtitle.complete"
    # 字幕整理失败
    SubtitleTransferFailed = "transfer.subtitle.failed"
    # 音频文件整理完成
    AudioTransferComplete = "transfer.audio.complete"
    # 音频文件整理失败
    AudioTransferFailed = "transfer.audio.failed"
    # 下载已添加
    DownloadAdded = "download.added"
    # 删除历史记录
    HistoryDeleted = "history.deleted"
    # 删除下载源文件
    DownloadFileDeleted = "downloadfile.deleted"
    # 删除下载任务
    DownloadDeleted = "download.deleted"
    # 收到用户外来消息
    UserMessage = "user.message"
    # 收到Webhook消息
    WebhookMessage = "webhook.message"
    # 发送消息通知
    NoticeMessage = "notice.message"
    # 订阅已添加
    SubscribeAdded = "subscribe.added"
    # 订阅已调整
    SubscribeModified = "subscribe.modified"
    # 订阅已删除
    SubscribeDeleted = "subscribe.deleted"
    # 订阅已完成
    SubscribeComplete = "subscribe.complete"
    # 系统错误
    SystemError = "system.error"
    # 刮削元数据
    MetadataScrape = "metadata.scrape"
    # 模块需要重载
    ModuleReload = "module.reload"
    # 配置项更新
    ConfigChanged = "config.updated"
    # 消息交互动作
    MessageAction = "message.action"
    # 执行工作流
    WorkflowExecute = "workflow.execute"
    # Agent Tokens 用量
    AgentTokensUsage = "agent.tokens.usage"


# EventType中文名称翻译字典
EVENT_TYPE_NAMES = {
    EventType.PluginReload: "插件重载",
    EventType.PluginAction: "触发插件动作",
    EventType.PluginTriggered: "触发插件事件",
    EventType.CommandExcute: "执行命令",
    EventType.SiteDeleted: "站点已删除",
    EventType.SiteUpdated: "站点已更新",
    EventType.SiteRefreshed: "站点已刷新",
    EventType.TransferComplete: "整理完成",
    EventType.TransferFailed: "整理失败",
    EventType.SubtitleTransferComplete: "字幕整理完成",
    EventType.SubtitleTransferFailed: "字幕整理失败",
    EventType.AudioTransferComplete: "音频整理完成",
    EventType.AudioTransferFailed: "音频整理失败",
    EventType.DownloadAdded: "添加下载",
    EventType.HistoryDeleted: "删除历史记录",
    EventType.DownloadFileDeleted: "删除下载源文件",
    EventType.DownloadDeleted: "删除下载任务",
    EventType.UserMessage: "收到用户消息",
    EventType.WebhookMessage: "收到Webhook消息",
    EventType.NoticeMessage: "发送消息通知",
    EventType.SubscribeAdded: "添加订阅",
    EventType.SubscribeModified: "订阅已调整",
    EventType.SubscribeDeleted: "订阅已删除",
    EventType.SubscribeComplete: "订阅已完成",
    EventType.SystemError: "系统错误",
    EventType.MetadataScrape: "刮削元数据",
    EventType.ModuleReload: "模块重载",
    EventType.ConfigChanged: "配置项更新",
    EventType.MessageAction: "消息交互动作",
    EventType.WorkflowExecute: "执行工作流",
    EventType.AgentTokensUsage: "Agent Tokens 用量",
}


# 同步链式事件
class ChainEventType(Enum):
    # 插件数据重置前
    PluginDataReset = "plugin.data.reset"
    # 名称识别
    NameRecognize = "name.recognize"
    # 音乐名称识别：插件辅助解析音乐标题中的曲名、艺术家、专辑、年份要素
    MusicNameRecognize = "music.name.recognize"
    # 媒体识别：原生识别未取得远端身份时，插件按已知要素匹配补充电影、电视剧媒体信息
    MediaRecognize = "media.recognize"
    # 音乐媒体识别：原生识别未取得远端身份时，插件按已知要素匹配补充音乐媒体信息
    MusicMediaRecognize = "music.media.recognize"
    # 认证验证
    AuthVerification = "auth.verification"
    # 认证拦截
    AuthIntercept = "auth.intercept"
    # 命令注册
    CommandRegister = "command.register"
    # 整理重命名
    TransferRename = "transfer.rename"
    # 整理重命名上下文构建
    TransferRenameBuild = "transfer.rename.build"
    # 整理拦截
    TransferIntercept = "transfer.intercept"
    # 整理覆盖检查
    TransferOverwriteCheck = "transfer.overwrite.check"
    # 资源选择
    ResourceSelection = "resource.selection"
    # 资源下载
    ResourceDownload = "resource.download"
    # 探索数据源
    DiscoverSource = "discover.source"
    # 媒体识别转换
    MediaRecognizeConvert = "media.recognize.convert"
    # 推荐数据源
    RecommendSource = "recommend.source"
    # 工作流执行
    WorkflowExecution = "workflow.execution"
    # 存储操作选择
    StorageOperSelection = "storage.operation"
    # Agent LLM 供应商选择
    AgentLLMProvider = "agent.llm.provider"
    # 订阅总集数刷新
    SubscribeEpisodesRefresh = "subscribe.episodes.refresh"
    # 订阅完成检查
    SubscribeCompletionCheck = "subscribe.completion.check"


# 系统配置Key字典
class SystemConfigKey(Enum):
    # 下载器配置
    Downloaders = "Downloaders"
    # 媒体服务器配置
    MediaServers = "MediaServers"
    # 消息通知配置
    Notifications = "Notifications"
    # 通知场景开关设置
    NotificationSwitchs = "NotificationSwitchs"
    # 目录配置
    Directories = "Directories"
    # 挂载型本地盘是否删除空目录
    MountedLocalDiskDeleteEmptyDirs = "MountedLocalDiskDeleteEmptyDirs"
    # 存储配置
    Storages = "Storages"
    # 搜索站点范围
    IndexerSites = "IndexerSites"
    # 订阅站点范围
    RssSites = "RssSites"
    # 自定义制作组/字幕组
    CustomReleaseGroups = "CustomReleaseGroups"
    # 自定义占位符
    Customization = "Customization"
    # 自定义识别词
    CustomIdentifiers = "CustomIdentifiers"
    # 集数定位规则词表
    EpisodeFormatRuleTable = "EpisodeFormatRuleTable"
    # 转移屏蔽词
    TransferExcludeWords = "TransferExcludeWords"
    # 种子优先级规则
    TorrentsPriority = "TorrentsPriority"
    # 用户自定义规则
    CustomFilterRules = "CustomFilterRules"
    # 用户规则组
    UserFilterRuleGroups = "UserFilterRuleGroups"
    # 搜索默认过滤规则组
    SearchFilterRuleGroups = "SearchFilterRuleGroups"
    # 订阅默认过滤规则组
    SubscribeFilterRuleGroups = "SubscribeFilterRuleGroups"
    # 订阅默认参数
    SubscribeDefaultParams = "SubscribeDefaultParams"
    # 洗版默认过滤规则组
    BestVersionFilterRuleGroups = "BestVersionFilterRuleGroups"
    # 订阅统计
    SubscribeReport = "SubscribeReport"
    # 用户自定义CSS
    UserCustomCSS = "UserCustomCSS"
    # 用户已安装的插件
    UserInstalledPlugins = "UserInstalledPlugins"
    # 共享源码插件的虚拟运行实例
    PluginInstances = "PluginInstances"
    # 插件文件夹分组配置
    PluginFolders = "PluginFolders"
    # 默认电影订阅规则
    DefaultMovieSubscribeConfig = "DefaultMovieSubscribeConfig"
    # 默认电视剧订阅规则
    DefaultTvSubscribeConfig = "DefaultTvSubscribeConfig"
    # 默认音乐订阅规则
    DefaultMusicSubscribeConfig = "DefaultMusicSubscribeConfig"
    # 用户站点认证参数
    UserSiteAuthParams = "UserSiteAuthParams"
    # Follow订阅分享者
    FollowSubscribers = "FollowSubscribers"
    # 通知发送时间
    NotificationSendTime = "NotificationSendTime"
    # AI智能体配置
    AIAgentConfig = "AIAgentConfig"
    # AI智能体外部MCP服务器配置
    AIAgentMcpServers = "AIAgentMcpServers"
    # 通知消息格式模板
    NotificationTemplates = "NotificationTemplates"
    # 通知中心清理时间
    NotificationClearBefore = "NotificationClearBefore"
    # 刮削开关设置
    ScrapingSwitchs = "ScrapingSwitchs"
    # 插件安装统计
    PluginInstallReport = "PluginInstallReport"
    # 绿联影视登录会话缓存
    UgreenSessionCache = "UgreenSessionCache"
    # 共享媒体识别成功次数
    MediaRecognizeShareCount = "MediaRecognizeShareCount"
    # 多媒体自动分类策略及其有界历史
    MediaClassificationPolicy = "MediaClassificationPolicy"


# 处理进度Key字典
class ProgressKey(Enum):
    # 搜索
    Search = "search"
    # 整理
    FileTransfer = "filetransfer"
    # 批量重命名
    BatchRename = "batchrename"


# 媒体图片类型
class MediaImageType(Enum):
    Poster = "poster_path"
    Backdrop = "backdrop_path"


# 消息类型
class MessageType(Enum):
    # 资源下载
    Download = "资源下载"
    # 整理入库
    Organize = "整理入库"
    # 订阅
    Subscribe = "订阅"
    # 站点消息
    SiteMessage = "站点"
    # 媒体服务器通知
    MediaServer = "媒体服务器"
    # 处理失败需要人工干预
    Manual = "手动处理"
    # 插件消息
    Plugin = "插件"
    # 智能体消息
    Agent = "智能体"
    # 其它消息
    Other = "其它"


class ContentType(str, Enum):
    """
    消息内容类型
    操作状态的通知消息类型标识
    """
    # 订阅添加成功
    SubscribeAdded = "subscribeAdded"
    # 订阅完成
    SubscribeComplete = "subscribeComplete"
    # 入库成功
    OrganizeSuccess = "organizeSuccess"
    # 下载开始(添加下载任务成功)
    DownloadAdded = "downloadAdded"


# 通知渠道
class NotificationChannel(Enum):
    """
    通知渠道
    """
    Wechat = "微信"
    Feishu = "飞书"
    WechatClawBot = "微信ClawBot"
    Telegram = "Telegram"
    Slack = "Slack"
    Discord = "Discord"
    DingTalk = "钉钉"
    SynologyChat = "SynologyChat"
    VoceChat = "VoceChat"
    Web = "Web"
    WebAgent = "WebAgent"
    WebPush = "WebPush"
    QQ = "QQ"


class NotificationAction(str, Enum):
    """
    通知渠道通用管理动作

    作为渠道管理契约的公共词汇表，具体动作的支持范围与参数语义由渠道模块自行解释
    """
    # 查询登录状态与二维码
    STATUS = "status"
    # 刷新登录二维码
    REFRESH_QRCODE = "refresh_qrcode"
    # 退出登录
    LOGOUT = "logout"
    # 测试连通性
    TEST_CONNECTION = "test_connection"
    # 迁移渠道名变更前的登录缓存
    MIGRATE_CACHE = "migrate_cache"
    # 同步通知配置变更产生的缓存迁移和清理
    RECONCILE_CONFIG = "reconcile_config"


class StorageAction(str, Enum):
    """
    网盘存储通用管理动作

    作为存储管理契约的公共词汇表，具体动作的支持范围与参数语义由存储实现自行解释
    """
    # 保存存储配置
    SAVE_CONFIG = "save_config"
    # 重置存储配置
    RESET_CONFIG = "reset_config"
    # 生成登录二维码
    GENERATE_QRCODE = "generate_qrcode"
    # 生成 OAuth2 授权 URL
    GENERATE_AUTH_URL = "generate_auth_url"
    # 登录确认
    CHECK_LOGIN = "check_login"
    # 查询存储空间用量
    USAGE = "usage"
    # 查询支持的整理方式
    SUPPORT_TRANSTYPE = "support_transtype"


# LLM 提供商通用管理动作
class LlmProviderAction(str, Enum):
    """
    LLM 提供商通用管理动作

    作为提供商管理契约的公共词汇表，具体动作的支持范围与参数语义由提供商实现自行解释
    """
    # 查询提供商目录
    LIST_PROVIDERS = "list_providers"
    # 查询模型目录
    LIST_MODELS = "list_models"
    # 启动授权会话
    START_AUTH = "start_auth"
    # 查询授权会话状态
    AUTH_STATUS = "auth_status"
    # 轮询授权会话
    POLL_AUTH = "poll_auth"
    # 断开授权
    DISCONNECT = "disconnect"
    # 测试调用
    TEST = "test"


# 下载器类型
class DownloaderType(Enum):
    # Qbittorrent
    Qbittorrent = "Qbittorrent"
    # Transmission
    Transmission = "Transmission"
    # Rtorrent
    Rtorrent = "Rtorrent"
    # Aria2
    # Aria2 = "Aria2"


# 媒体服务器类型
class MediaServerType(Enum):
    # Emby
    Emby = "Emby"
    # 极影视
    ZSpace = "ZSpace"
    # Jellyfin
    Jellyfin = "Jellyfin"
    # Plex
    Plex = "Plex"
    # 飞牛影视
    TrimeMedia = "TrimeMedia"
    # 绿联影视
    Ugreen = "Ugreen"
    # Navidrome 音乐服务器
    Navidrome = "Navidrome"


# 识别器类型
class MediaRecognizeType(Enum):
    # 豆瓣
    Douban = "豆瓣"
    # TMDB
    TMDB = "TheMovieDb"
    # TVDB
    TVDB = "TheTvDb"
    # bangumi
    Bangumi = "Bangumi"
    # AniList
    AniList = "AniList"
    # IMDb
    IMDb = "IMDb"
    # MusicBrainz
    MusicBrainz = "MusicBrainz"
    # TheAudioDB
    TheAudioDB = "TheAudioDB"


# 用户配置Key字典
class UserConfigKey(Enum):
    # 监控面板
    Dashboard = "Dashboard"


# 支持的存储类型
class StorageSchema(Enum):
    # 存储类型
    Local = "local"
    Alipan = "alipan"
    U115 = "u115"
    Rclone = "rclone"
    Alist = "alist"
    AlistGo = "alistgo"
    SMB = "smb"


# 模块类型
class ModuleType(Enum):
    # 下载器
    Downloader = "downloader"
    # 媒体服务器
    MediaServer = "mediaserver"
    # 消息服务
    Notification = "notification"
    # 媒体识别
    MediaRecognize = "mediarecognize"
    # 站点索引
    Indexer = "indexer"
    # 其它
    Other = "other"


# 其他杂项模块类型
class OtherModulesType(Enum):
    # 字幕
    Subtitle = "站点字幕"
    # Fanart
    Fanart = "Fanart"
    # 文件整理
    FileManager = "文件整理"
    # 过滤器
    Filter = "过滤器"
    # 站点索引
    Indexer = "站点索引"
    # PostgreSQL
    PostgreSQL = "PostgreSQL"
    # Redis
    Redis = "Redis"
    # ListenBrainz
    ListenBrainz = "ListenBrainz"
    # LRCLIB 歌词
    Lrclib = "LRCLIB"
    # Musixmatch 授权歌词
    Musixmatch = "Musixmatch"
    # AcoustID 音频指纹
    AcoustId = "AcoustID"


class NameValueEnum(Enum):
    """支持通过 name 或 value 实例化的枚举基类"""

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            for member in cls:
                if member.name.lower() == value.lower() or member.value == value:
                    return member
        return None


# 刮削策略
class ScrapingPolicy(NameValueEnum):
    MISSINGONLY = "仅缺失"
    SKIP = "跳过"
    OVERWRITE = "覆盖"
    UPGRADE = "质量升级"


# 刮削目标类型
class ScrapingTarget(NameValueEnum):
    MOVIE = "电影"
    TV = "电视剧"
    SEASON = "季"
    EPISODE = "集"
    MUSIC = "音乐"


# 刮削元数据类型
class ScrapingMetadata(NameValueEnum):
    NFO = "NFO"
    POSTER = "海报"
    BACKDROP = "背景图"
    LOGO = "Logo"
    BANNER = "横幅图"
    THUMB = "缩略图"
    DISC = "光盘图"
    CLEARART = "透明艺术图"
    LANDSCAPE = "横版缩略图"
    LYRICS = "歌词"
