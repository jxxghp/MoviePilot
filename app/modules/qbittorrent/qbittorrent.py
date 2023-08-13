import time
from typing import Optional, Union, Tuple, List

import qbittorrentapi
from qbittorrentapi import TorrentDictionary, TorrentFilesList
from qbittorrentapi.client import Client
from qbittorrentapi.transfer import TransferInfoDictionary

from app.core.config import settings
from app.log import logger
from app.utils.singleton import Singleton
from app.utils.string import StringUtils


class Qbittorrent(metaclass=Singleton):
    _host: str = None
    _port: int = None
    _username: str = None
    _passowrd: str = None

    qbc: Client = None

    def __init__(self):
        self._host, self._port = StringUtils.get_domain_address(address=settings.QB_HOST, prefix=True)
        self._username = settings.QB_USER
        self._password = settings.QB_PASSWORD
        if self._host and self._port and self._username and self._password:
            self.qbc = self.__login_qbittorrent()

    def __login_qbittorrent(self) -> Optional[Client]:
        """
        连接qbittorrent
        :return: qbittorrent对象
        """
        try:
            # 登录
            qbt = qbittorrentapi.Client(host=self._host,
                                        port=self._port,
                                        username=self._username,
                                        password=self._password,
                                        VERIFY_WEBUI_CERTIFICATE=False,
                                        REQUESTS_ARGS={'timeout': (15, 60)})
            try:
                qbt.auth_log_in()
            except qbittorrentapi.LoginFailed as e:
                logger.error(f"qbittorrent 登录失败：{e}")
            return qbt
        except Exception as err:
            logger.error(f"qbittorrent 连接出错：{err}")
            return None

    def get_torrents(self, ids: Union[str, list] = None,
                     status: Union[str, list] = None,
                     tags: Union[str, list] = None) -> Tuple[List[TorrentDictionary], bool]:
        """
        获取种子列表
        return: 种子列表, 是否发生异常
        """
        if not self.qbc:
            return [], True
        try:
            torrents = self.qbc.torrents_info(torrent_hashes=ids,
                                              status_filter=status)
            if tags:
                results = []
                if not isinstance(tags, list):
                    tags = [tags]
                for torrent in torrents:
                    torrent_tags = [str(tag).strip() for tag in torrent.get("tags").split(',')]
                    if set(tags).issubset(set(torrent_tags)):
                        results.append(torrent)
                return results, False
            return torrents or [], False
        except Exception as err:
            logger.error(f"获取种子列表出错：{err}")
            return [], True

    def get_completed_torrents(self, ids: Union[str, list] = None,
                               tags: Union[str, list] = None) -> Optional[List[TorrentDictionary]]:
        """
        获取已完成的种子
        return: 种子列表, 如发生异常则返回None
        """
        if not self.qbc:
            return None
        torrents, error = self.get_torrents(status=["completed"], ids=ids, tags=tags)
        return None if error else torrents or []

    def get_downloading_torrents(self, ids: Union[str, list] = None,
                                 tags: Union[str, list] = None) -> Optional[List[TorrentDictionary]]:
        """
        获取正在下载的种子
        return: 种子列表, 如发生异常则返回None
        """
        if not self.qbc:
            return None
        torrents, error = self.get_torrents(ids=ids,
                                            status=["downloading"],
                                            tags=tags)
        return None if error else torrents or []

    def remove_torrents_tag(self, ids: Union[str, list], tag: Union[str, list]) -> bool:
        """
        移除种子Tag
        :param ids: 种子Hash列表
        :param tag: 标签内容
        """
        if not self.qbc:
            return False
        try:
            self.qbc.torrents_delete_tags(torrent_hashes=ids, tags=tag)
            return True
        except Exception as err:
            logger.error(f"移除种子Tag出错：{err}")
            return False

    def set_torrents_tag(self, ids: Union[str, list], tags: list):
        """
        设置种子状态为已整理，以及是否强制做种
        """
        if not self.qbc:
            return
        try:
            # 打标签
            self.qbc.torrents_add_tags(tags=tags, torrent_hashes=ids)
        except Exception as err:
            logger.error(f"设置种子Tag出错：{err}")

    def torrents_set_force_start(self, ids: Union[str, list]):
        """
        设置强制作种
        """
        if not self.qbc:
            return
        try:
            self.qbc.torrents_set_force_start(enable=True, torrent_hashes=ids)
        except Exception as err:
            logger.error(f"设置强制作种出错：{err}")

    def __get_last_add_torrentid_by_tag(self, tags: Union[str, list],
                                        status: Union[str, list] = None) -> Optional[str]:
        """
        根据种子的下载链接获取下载中或暂停的钟子的ID
        :return: 种子ID
        """
        try:
            torrents, _ = self.get_torrents(status=status, tags=tags)
        except Exception as err:
            logger.error(f"获取种子列表出错：{err}")
            return None
        if torrents:
            return torrents[0].get("hash")
        else:
            return None

    def get_torrent_id_by_tag(self, tags: Union[str, list],
                              status: Union[str, list] = None) -> Optional[str]:
        """
        通过标签多次尝试获取刚添加的种子ID，并移除标签
        """
        torrent_id = None
        # QB添加下载后需要时间，重试5次每次等待5秒
        for i in range(1, 10):
            time.sleep(3)
            torrent_id = self.__get_last_add_torrentid_by_tag(tags=tags,
                                                              status=status)
            if torrent_id is None:
                continue
            else:
                self.remove_torrents_tag(torrent_id, tags)
                break
        return torrent_id

    def add_torrent(self,
                    content: Union[str, bytes],
                    is_paused: bool = False,
                    download_dir: str = None,
                    tag: Union[str, list] = None,
                    cookie=None
                    ) -> bool:
        """
        添加种子
        :param content: 种子urls或文件内容
        :param is_paused: 添加后暂停
        :param tag: 标签
        :param download_dir: 下载路径
        :param cookie: 站点Cookie用于辅助下载种子
        :return: bool
        """
        if not self.qbc or not content:
            return False

        if isinstance(content, str):
            urls = content
            torrent_files = None
        else:
            urls = None
            torrent_files = content

        if download_dir:
            save_path = download_dir
            is_auto = False
        else:
            save_path = None
            is_auto = None

        if tag:
            tags = tag
        else:
            tags = None

        try:

            # 添加下载
            qbc_ret = self.qbc.torrents_add(urls=urls,
                                            torrent_files=torrent_files,
                                            save_path=save_path,
                                            is_paused=is_paused,
                                            tags=tags,
                                            use_auto_torrent_management=is_auto,
                                            cookie=cookie)
            return True if qbc_ret and str(qbc_ret).find("Ok") != -1 else False
        except Exception as err:
            logger.error(f"添加种子出错：{err}")
            return False

    def start_torrents(self, ids: Union[str, list]) -> bool:
        """
        启动种子
        """
        if not self.qbc:
            return False
        try:
            self.qbc.torrents_resume(torrent_hashes=ids)
            return True
        except Exception as err:
            logger.error(f"启动种子出错：{err}")
            return False

    def stop_torrents(self, ids: Union[str, list]) -> bool:
        """
        暂停种子
        """
        if not self.qbc:
            return False
        try:
            self.qbc.torrents_pause(torrent_hashes=ids)
            return True
        except Exception as err:
            logger.error(f"暂停种子出错：{err}")
            return False

    def delete_torrents(self, delete_file: bool, ids: Union[str, list]) -> bool:
        """
        删除种子
        """
        if not self.qbc:
            return False
        if not ids:
            return False
        try:
            self.qbc.torrents_delete(delete_files=delete_file, torrent_hashes=ids)
            return True
        except Exception as err:
            logger.error(f"删除种子出错：{err}")
            return False

    def get_files(self, tid: str) -> Optional[TorrentFilesList]:
        """
        获取种子文件清单
        """
        if not self.qbc:
            return None
        try:
            return self.qbc.torrents_files(torrent_hash=tid)
        except Exception as err:
            logger.error(f"获取种子文件列表出错：{err}")
            return None

    def set_files(self, **kwargs) -> bool:
        """
        设置下载文件的状态，priority为0为不下载，priority为1为下载
        """
        if not self.qbc:
            return False
        if not kwargs.get("torrent_hash") or not kwargs.get("file_ids"):
            return False
        try:
            self.qbc.torrents_file_priority(torrent_hash=kwargs.get("torrent_hash"),
                                            file_ids=kwargs.get("file_ids"),
                                            priority=kwargs.get("priority"))
            return True
        except Exception as err:
            logger.error(f"设置种子文件状态出错：{err}")
            return False

    def transfer_info(self) -> Optional[TransferInfoDictionary]:
        """
        获取传输信息
        """
        if not self.qbc:
            return None
        try:
            return self.qbc.transfer_info()
        except Exception as err:
            logger.error(f"获取传输信息出错：{err}")
            return None

    def set_speed_limit(self, download_limit: float = None, upload_limit: float = None) -> bool:
        """
        设置速度限制
        :param download_limit: 下载速度限制，单位KB/s
        :param upload_limit: 上传速度限制，单位kB/s
        """
        if not self.qbc:
            return False
        download_limit = download_limit * 1024
        upload_limit = upload_limit * 1024
        try:
            self.qbc.transfer.upload_limit = int(upload_limit)
            self.qbc.transfer.download_limit = int(download_limit)
        except Exception as err:
            logger.error(f"设置速度限制出错：{err}")
            return False

    def recheck_torrents(self, ids: Union[str, list]):
        """
        重新校验种子
        """
        if not self.qbc:
            return False
        try:
            return self.qbc.torrents_recheck(torrent_hashes=ids)
        except Exception as err:
            logger.error(f"重新校验种子出错：{err}")
            return False
