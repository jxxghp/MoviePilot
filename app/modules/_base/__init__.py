"""模块业务样板基类包。

沉淀各内置模块逐字复制的业务样板，模块发现规则
（`ModuleHelper.load`）会跳过 `_` 前缀的包与类，因此本包不会被识别为可实例化模块。
"""

from app.modules._base.downloader import _DownloaderModuleBase
from app.modules._base.mediaserver import _MediaServerModuleBase
from app.modules._base.notification import _MessageChannelModuleBase
from app.modules._base.storage import _StorageModuleBase

__all__ = [
    "_DownloaderModuleBase",
    "_MessageChannelModuleBase",
    "_MediaServerModuleBase",
    "_StorageModuleBase",
]
