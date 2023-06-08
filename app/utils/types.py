from enum import Enum


class MediaType(Enum):
    MOVIE = '电影'
    TV = '电视剧'
    UNKNOWN = '未知'


class TorrentStatus(Enum):
    TRANSFER = "可转移"


# 可监听事件
class EventType(Enum):
    # 插件重载
    PluginReload = "plugin.reload"
    # 执行命令
    CommandExcute = "command.excute"
    # 站点签到
    SiteSignin = "site.signin"


# 系统配置Key字典
class SystemConfigKey(Enum):
    # 用户已安装的插件
    UserInstalledPlugins = "UserInstalledPlugins"


# 站点框架
class SiteSchema(Enum):
    DiscuzX = "Discuz!"
    Gazelle = "Gazelle"
    Ipt = "IPTorrents"
    NexusPhp = "NexusPhp"
    NexusProject = "NexusProject"
    NexusRabbit = "NexusRabbit"
    SmallHorse = "Small Horse"
    Unit3d = "Unit3d"
    TorrentLeech = "TorrentLeech"
    FileList = "FileList"
    TNode = "TNode"
