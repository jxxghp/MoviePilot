from app.helper.servicebase import ServiceBaseHelper
from app.schemas import DownloaderConf
from app.schemas.types import SystemConfigKey


class DownloaderHelper(ServiceBaseHelper[DownloaderConf]):
    """
    下载器帮助类
    """

    def __init__(self):
        super().__init__(
            config_key=SystemConfigKey.Downloaders,
            conf_type=DownloaderConf,
            modules=["QbittorrentModule", "TransmissionModule"]
        )
