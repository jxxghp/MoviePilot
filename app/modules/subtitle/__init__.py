from pathlib import Path
from typing import Tuple, Union

from app.core.context import Context
from app.modules import _ModuleBase


class SubtitleModule(_ModuleBase):
    """
    字幕下载模块
    """

    def init_module(self) -> None:
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def stop(self) -> None:
        pass

    def download_added(self, context: Context, torrent_path: Path) -> None:
        """
        添加下载任务成功后，从站点下载字幕
        :param context:  上下文，包括识别信息、媒体信息、种子信息
        :param torrent_path:  种子文件地址
        :return: None，该方法可被多个模块同时处理
        """
        pass

