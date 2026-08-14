"""
ORM 模型。

_identity 必须在此处导入：它在 import 期把媒体身份归一挂到 mapper 事件上，是六张带
身份列的表的写入不变量。导入任一模型都会先初始化本包，因此这一行让强制点无处可绕。
"""
from . import _identity  # noqa: F401  仅为注册 mapper 事件，不导出符号
from .agentchat import AgentChat
from .agenttask import AgentTask
from .agenttaskrun import AgentTaskRun
from .downloadfailure import DownloadFailure
from .downloadhistory import DownloadHistory, DownloadFiles
from .mediaserver import MediaServerItem
from .message import Message
from .passkey import PassKey
from .plugindata import PluginData
from .site import Site
from .siteicon import SiteIcon
from .sitestatistic import SiteStatistic
from .siteuserdata import SiteUserData
from .subscribe import Subscribe
from .subscribehistory import SubscribeHistory
from .systemconfig import SystemConfig
from .transferhistory import TransferHistory
from .transferpending import TransferPending
from .user import User
from .userconfig import UserConfig
from .workflow import Workflow
