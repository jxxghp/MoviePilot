from typing import Optional, Tuple, Union

from app.core import MediaInfo
from app.modules import _ModuleBase


class FileTransferModule(_ModuleBase):

    def init_module(self) -> None:
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "TRANSFER_TYPE", True

    def transfer(self, path: str, mediainfo: MediaInfo) -> Optional[bool]:
        """
        TODO 文件转移
        :param path:  文件路径
        :param mediainfo:  识别的媒体信息
        :return: 成功或失败
        """
        pass
