from pathlib import Path
from typing import Optional, Union, Tuple, List

import transmission_rpc
from transmission_rpc import Client, Torrent, File

from app.core import settings
from app.log import logger
from app.utils.singleton import Singleton


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
        host = settings.TR_HOST
        if host and host.find(":") != -1:
            self._host = settings.TR_HOST.split(":")[0]
            self._port = settings.TR_HOST.split(":")[1]
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
                     tag: Union[str, list] = None) -> Tuple[List[Torrent], bool]:
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
        if tag and not isinstance(tag, list):
            tag = [tag]
        ret_torrents = []
        for torrent in torrents:
            if status and torrent.status not in status:
                continue
            labels = torrent.labels if hasattr(torrent, "labels") else []
            include_flag = True
            if tag:
                for t in tag:
                    if t and t not in labels:
                        include_flag = False
                        break
            if include_flag:
                ret_torrents.append(torrent)
        return ret_torrents, False

    def get_completed_torrents(self, ids: Union[str, list] = None,
                               tag: Union[str, list] = None) -> Optional[List[Torrent]]:
        """
        获取已完成的种子列表
        return 种子列表, 发生错误时返回None
        """
        if not self.trc:
            return None
        try:
            torrents, error = self.get_torrents(status=["seeding", "seed_pending"], ids=ids, tag=tag)
            return None if error else torrents or []
        except Exception as err:
            logger.error(f"获取已完成的种子列表出错：{err}")
            return None

    def get_downloading_torrents(self, ids: Union[str, list] = None,
                                 tag: Union[str, list] = None) -> Optional[List[Torrent]]:
        """
        获取正在下载的种子列表
        return 种子列表, 发生错误时返回None
        """
        if not self.trc:
            return None
        try:
            torrents, error = self.get_torrents(ids=ids,
                                                status=["downloading", "download_pending"],
                                                tag=tag)
            return None if error else torrents or []
        except Exception as err:
            logger.error(f"获取正在下载的种子列表出错：{err}")
            return None

    def set_torrent_tag(self, ids: str, tag: list) -> bool:
        """
        设置种子标签
        """
        if not ids or not tag:
            return False
        try:
            self.trc.change_torrent(labels=tag, ids=ids)
            return True
        except Exception as err:
            logger.error(f"设置种子标签出错：{err}")
            return False

    def get_transfer_torrents(self, tag: Union[str, list] = None) -> List[dict]:
        """
        获取下载文件转移任务种子
        """
        # 处理下载完成的任务
        torrents = self.get_completed_torrents() or []
        trans_tasks = []
        for torrent in torrents:
            # 3.0版本以下的Transmission没有labels
            if not hasattr(torrent, "labels"):
                logger.error(f"版本可能过低，无labels属性，请安装3.0以上版本！")
                break
            torrent_tags = torrent.labels or ""
            # 含"已整理"tag的不处理
            if "已整理" in torrent_tags:
                continue
            # 开启标签隔离，未包含指定标签的不处理
            if tag and tag not in torrent_tags:
                logger.debug(f"{torrent.name} 未包含指定标签：{tag}")
                continue
            path = torrent.download_dir
            # 无法获取下载路径的不处理
            if not path:
                logger.debug(f"未获取到 {torrent.name} 下载保存路径")
                continue
            trans_tasks.append({
                'title': torrent.name,
                'path': Path(path) / torrent.name,
                'id': torrent.hashString,
                'tags': torrent.labels
            })
        return trans_tasks

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

    def set_files(self, **kwargs) -> bool:
        """
        设置下载文件的状态
        {
            <torrent id>: {
                <file id>: {
                    'priority': <priority ('high'|'normal'|'low')>,
                    'selected': <selected for download (True|False)>
                },
                ...
            },
            ...
        }
        """
        if not kwargs.get("file_info"):
            return False
        try:
            self.trc.set_files(kwargs.get("file_info"))
            return True
        except Exception as err:
            logger.error(f"设置下载文件状态出错：{err}")
            return False
