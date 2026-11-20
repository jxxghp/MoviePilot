"""
插件可使用的稳定 Chain 基类入口。

暂停下载任务时，插件应通过 ``ChainBase.stop_torrents`` 声明暂停意图，MP 之后只按意图处理：
``ABANDON`` 放弃（订阅换种子）、``DEFER`` 暂缓（不恢复，等插件自己恢复）、``RESUMABLE`` 可恢复
（订阅需要时 MP 可直接恢复）。例如 ``stop_torrents(hashs, downloader, actor=DownloadTaskActor.PLUGIN,
intent=DownloadPauseIntent.DEFER, plugin_id=self.__class__.__name__)``；直接调用下载器客户端暂停的任务
没有 MP 暂停标签，会被当成外部暂停（意图未知，不会被恢复）。
"""

from app.chain.base import ChainBase
from app.schemas.download import DownloadPauseIntent, DownloadTaskActor

__all__ = ["ChainBase", "DownloadPauseIntent", "DownloadTaskActor"]
