from typing import Optional, Union, Tuple, List

import transmission_rpc
from transmission_rpc import Client, Torrent, File
from transmission_rpc.session import SessionStats

from app.core.config import settings
from app.log import logger
from app.utils.singleton import Singleton
from app.utils.string import StringUtils


class Transmission(metaclass=Singleton):
    _host: str = None
    _port: int = None
    _username: str = None
    _passowrd: str = None

    trc: Optional[Client] = None

    # 参考transmission web，仅查询需要的参数，加速种子搜索
    _trarg = ["id", "name", "status", "labels", "hashString", "totalSize", "percentDone", "addedDate", "trackerStats",
              "leftUntilDone", "rateDownload", "rateUpload", "recheckProgress", "rateDownload", "rateUpload",
              "peersGettingFromUs", "peersSendingToUs", "uploadRatio", "uploadedEver", "downloadedEver", "downloadDir",
              "error", "errorString", "doneDate", "queuePosition", "activityDate", "trackers"]

    def __init__(self):
        self._host, self._port = StringUtils.get_domain_address(address=settings.TR_HOST, prefix=False)
        self._username = settings.TR_USER
        self._password = settings.TR_PASSWORD
        if self._host and self._port and self._username and self._password:
            self.trc = self.__login_transmission()

    def __login_transmission(self) -> Optional[Client]:
        """
        连接transmission
        :return: transmission对象
        """
        try:
            # 登录
            trt = transmission_rpc.Client(host=self._host,
                                          port=self._port,
                                          username=self._username,
                                          password=self._password,
                                          timeout=60)
            return trt
        except Exception as err:
            logger.error(f"transmission 连接出错：{err}")
            return None

    def get_torrents(self, ids: Union[str, list] = None, status: Union[str, list] = None,
                     tags: Union[str, list] = None) -> Tuple[List[Torrent], bool]:
        """
        获取种子列表
        返回结果 种子列表, 是否有错误
        """
        if not self.trc:
            return [], True
        try:
            torrents = self.trc.get_torrents(ids=ids, arguments=self._trarg)
        except Exception as err:
            logger.error(f"获取种子列表出错：{err}")
            return [], True
        if status and not isinstance(status, list):
            status = [status]
        if tags and not isinstance(tags, list):
            tags = [tags]
        ret_torrents = []
        for torrent in torrents:
            # 状态过滤
            if status and torrent.status not in status:
                continue
            # 种子标签
            labels = [str(tag).strip()
                      for tag in torrent.labels] if hasattr(torrent, "labels") else []
            if tags and not set(tags).issubset(set(labels)):
                continue
            ret_torrents.append(torrent)
        return ret_torrents, False

    def get_completed_torrents(self, ids: Union[str, list] = None,
                               tags: Union[str, list] = None) -> Optional[List[Torrent]]:
        """
        获取已完成的种子列表
        return 种子列表, 发生错误时返回None
        """
        if not self.trc:
            return None
        try:
            torrents, error = self.get_torrents(status=["seeding", "seed_pending"], ids=ids, tags=tags)
            return None if error else torrents or []
        except Exception as err:
            logger.error(f"获取已完成的种子列表出错：{err}")
            return None

    def get_downloading_torrents(self, ids: Union[str, list] = None,
                                 tags: Union[str, list] = None) -> Optional[List[Torrent]]:
        """
        获取正在下载的种子列表
        return 种子列表, 发生错误时返回None
        """
        if not self.trc:
            return None
        try:
            torrents, error = self.get_torrents(ids=ids,
                                                status=["downloading", "download_pending", "stopped"],
                                                tags=tags)
            return None if error else torrents or []
        except Exception as err:
            logger.error(f"获取正在下载的种子列表出错：{err}")
            return None

    def set_torrent_tag(self, ids: str, tags: list) -> bool:
        """
        设置种子标签
        """
        if not self.trc:
            return False
        if not ids or not tags:
            return False
        try:
            self.trc.change_torrent(labels=tags, ids=ids)
            return True
        except Exception as err:
            logger.error(f"设置种子标签出错：{err}")
            return False

    def add_torrent(self, content: Union[str, bytes],
                    is_paused: bool = False,
                    download_dir: str = None,
                    labels=None,
                    cookie=None) -> Optional[Torrent]:
        """
        添加下载任务
        :param content: 种子urls或文件内容
        :param is_paused: 添加后暂停
        :param download_dir: 下载路径
        :param labels: 标签
        :param cookie: 站点Cookie用于辅助下载种子
        :return: Torrent
        """
        if not self.trc:
            return None
        try:
            return self.trc.add_torrent(torrent=content,
                                        download_dir=download_dir,
                                        paused=is_paused,
                                        labels=labels,
                                        cookies=cookie)
        except Exception as err:
            logger.error(f"添加种子出错：{err}")
            return None

    def start_torrents(self, ids: Union[str, list]) -> bool:
        """
        启动种子
        """
        if not self.trc:
            return False
        try:
            self.trc.start_torrent(ids=ids)
            return True
        except Exception as err:
            logger.error(f"启动种子出错：{err}")
            return False

    def stop_torrents(self, ids: Union[str, list]) -> bool:
        """
        停止种子
        """
        if not self.trc:
            return False
        try:
            self.trc.stop_torrent(ids=ids)
            return True
        except Exception as err:
            logger.error(f"停止种子出错：{err}")
            return False

    def delete_torrents(self, delete_file: bool, ids: Union[str, list]) -> bool:
        """
        删除种子
        """
        if not self.trc:
            return False
        if not ids:
            return False
        try:
            self.trc.remove_torrent(delete_data=delete_file, ids=ids)
            return True
        except Exception as err:
            logger.error(f"删除种子出错：{err}")
            return False

    def get_files(self, tid: str) -> Optional[List[File]]:
        """
        获取种子文件列表
        """
        if not self.trc:
            return None
        if not tid:
            return None
        try:
            torrent = self.trc.get_torrent(tid)
        except Exception as err:
            logger.error(f"获取种子文件列表出错：{err}")
            return None
        if torrent:
            return torrent.files()
        else:
            return None

    def set_files(self, tid: str, file_ids: list) -> bool:
        """
        设置下载文件的状态
        """
        if not self.trc:
            return False
        try:
            self.trc.change_torrent(ids=tid, files_wanted=file_ids)
            return True
        except Exception as err:
            logger.error(f"设置下载文件状态出错：{err}")
            return False

    def transfer_info(self) -> Optional[SessionStats]:
        """
        获取传输信息
        """
        if not self.trc:
            return None
        try:
            return self.trc.session_stats()
        except Exception as err:
            logger.error(f"获取传输信息出错：{err}")
            return None

    def set_speed_limit(self, download_limit: float = None, upload_limit: float = None):
        """
        设置速度限制
        :param download_limit: 下载速度限制，单位KB/s
        :param upload_limit: 上传速度限制，单位kB/s
        """
        if not self.trc:
            return
        try:
            session = self.trc.get_session()
            download_limit_enabled = True if download_limit else False
            upload_limit_enabled = True if upload_limit else False
            self.trc.set_session(
                speed_limit_down=int(download_limit),
                speed_limit_up=int(upload_limit),
                speed_limit_down_enabled=download_limit_enabled,
                speed_limit_up_enabled=upload_limit_enabled
            )
        except Exception as err:
            logger.error(f"设置速度限制出错：{err}")
            return False

    def recheck_torrents(self, ids: Union[str, list]):
        """
        重新校验种子
        """
        if not self.trc:
            return False
        try:
            return self.trc.verify_torrent(ids=ids)
        except Exception as err:
            logger.error(f"重新校验种子出错：{err}")
            return False
