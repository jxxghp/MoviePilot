from pathlib import Path
from typing import Tuple, Union

from app.core.context import Context
from app.modules import _ModuleBase


class WordseModule(_ModuleBase):
    """
    字幕下载模块
    """

    def init_module(self) -> None:
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def stop(self) -> None:
        pass

    def prepare_recognize(self, title: str,
                          subtitle: str = None) -> Tuple[str, str]:
        """
        处理各类特别命名，以便识别
        :param title:     标题
        :param subtitle:  副标题
        :return: 处理后的标题、副标题，该方法可被多个模块同时处理
        """
        pass
