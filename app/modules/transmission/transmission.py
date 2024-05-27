from typing import Optional, Union, Tuple, List, Dict

import transmission_rpc
from transmission_rpc import Client, Torrent, File
from transmission_rpc.session import SessionStats, Session

from app.core.config import settings
from app.log import logger
from app.utils.string import StringUtils


class Transmission:
    _host: str = None
    _port: int = None
    _username: str = None
    _password: str = None

    trc: Optional[Client] = None

    # 参考transmission web，仅查询需要的参数，加速种子搜索
    _trarg = ["id", "name", "status", "labels", "hashString", "totalSize", "percentDone", "addedDate", "trackerList",
              "trackerStats",
              "leftUntilDone", "rateDownload", "rateUpload", "recheckProgress", "rateDownload", "rateUpload",
              "peersGettingFromUs", "peersSendingToUs", "uploadRatio", "uploadedEver", "downloadedEver", "downloadDir",
              "error", "errorString", "doneDate", "queuePosition", "activityDate", "trackers"]

    def __init__(self, host: str = None, port: int = None, username: str = None, password: str = None):
        """
        若不设置参数，则创建配置文件设置的下载器
        """
        if host and port:
            self._host, self._port = host, port
        else:
            self._host, self._port = StringUtils.get_domain_address(address=settings.TR_HOST, prefix=False)
        self._username = username if username else settings.TR_USER
        self._password = password if password else settings.TR_PASSWORD
        if self._host and self._port:
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
            logger.error(f"transmission 连接出错：{str(err)}")
            return None

    def is_inactive(self) -> bool:
        """
        判断是否需要重连
        """
        if not self._host or not self._port:
            return False
        return True if not self.trc else False

    def reconnect(self):
        """
        重连
        """
        self.trc = self.__login_transmission()

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
            logger.error(f"获取种子列表出错：{str(err)}")
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
            logger.error(f"获取已完成的种子列表出错：{str(err)}")
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
            logger.error(f"获取正在下载的种子列表出错：{str(err)}")
            return None

    def set_torrent_tag(self, ids: str, tags: list, org_tags: list = None) -> bool:
        """
        设置种子标签，注意TR默认会覆盖原有标签，如需追加需传入原有标签
        """
        if not self.trc:
            return False
        if not ids or not tags:
            return False
        try:
            self.trc.change_torrent(labels=list(set((org_tags or []) + tags)), ids=ids)
            return True
        except Exception as err:
            logger.error(f"设置种子标签出错：{str(err)}")
            return False

    def get_torrent_tags(self, ids: str) -> List[str]:
        """
        获取所有种子标签
        """
        if not self.trc:
            return []
        try:
            torrent = self.trc.get_torrents(ids=ids, arguments=self._trarg)
            if torrent:
                labels = [str(tag).strip()
                          for tag in torrent.labels] if hasattr(torrent, "labels") else []
                return labels
        except Exception as err:
            logger.error(f"获取种子标签出错：{str(err)}")
            return []
        return []

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
            logger.error(f"添加种子出错：{str(err)}")
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
            logger.error(f"启动种子出错：{str(err)}")
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
            logger.error(f"停止种子出错：{str(err)}")
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
            logger.error(f"删除种子出错：{str(err)}")
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
            logger.error(f"获取种子文件列表出错：{str(err)}")
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
            logger.error(f"设置下载文件状态出错：{str(err)}")
            return False

    def set_unwanted_files(self, tid: str, file_ids: list) -> bool:
        """
        设置下载文件的状态
        """
        if not self.trc:
            return False
        try:
            self.trc.change_torrent(ids=tid, files_unwanted=file_ids)
            return True
        except Exception as err:
            logger.error(f"设置下载文件状态出错：{str(err)}")
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
            logger.error(f"获取传输信息出错：{str(err)}")
            return None

    def set_speed_limit(self, download_limit: float = None, upload_limit: float = None) -> bool:
        """
        设置速度限制
        :param download_limit: 下载速度限制，单位KB/s
        :param upload_limit: 上传速度限制，单位kB/s
        """
        if not self.trc:
            return False
        try:
            download_limit_enabled = True if download_limit else False
            upload_limit_enabled = True if upload_limit else False
            self.trc.set_session(
                speed_limit_down=int(download_limit),
                speed_limit_up=int(upload_limit),
                speed_limit_down_enabled=download_limit_enabled,
                speed_limit_up_enabled=upload_limit_enabled
            )
            return True
        except Exception as err:
            logger.error(f"设置速度限制出错：{str(err)}")
            return False

    def get_speed_limit(self) -> Optional[Tuple[float, float]]:
        """
        获取TR速度
        :return: download_limit 下载速度 默认是0
                 upload_limit 上传速度   默认是0
        """
        if not self.trc:
            return None

        download_limit = 0
        upload_limit = 0
        try:
            download_limit = self.trc.get_session().get('speed_limit_down')
            upload_limit = self.trc.get_session().get('speed_limit_up')

        except Exception as err:
            logger.error(f"获取速度限制出错：{str(err)}")

        return (
            download_limit,
            upload_limit
        )

    def recheck_torrents(self, ids: Union[str, list]) -> bool:
        """
        重新校验种子
        """
        if not self.trc:
            return False
        try:
            self.trc.verify_torrent(ids=ids)
            return True
        except Exception as err:
            logger.error(f"重新校验种子出错：{str(err)}")
            return False

    def change_torrent(self,
                       hash_string: str,
                       upload_limit=None,
                       download_limit=None,
                       ratio_limit=None,
                       seeding_time_limit=None) -> bool:
        """
        设置种子
        :param hash_string: ID
        :param upload_limit: 上传限速 Kb/s
        :param download_limit: 下载限速 Kb/s
        :param ratio_limit: 分享率限制
        :param seeding_time_limit: 做种时间限制
        :return: bool
        """
        if not hash_string:
            return False
        if upload_limit:
            uploadLimited = True
            uploadLimit = int(upload_limit)
        else:
            uploadLimited = False
            uploadLimit = 0
        if download_limit:
            downloadLimited = True
            downloadLimit = int(download_limit)
        else:
            downloadLimited = False
            downloadLimit = 0
        if ratio_limit:
            seedRatioMode = 1
            seedRatioLimit = round(float(ratio_limit), 2)
        else:
            seedRatioMode = 2
            seedRatioLimit = 0
        if seeding_time_limit:
            seedIdleMode = 1
            seedIdleLimit = int(seeding_time_limit)
        else:
            seedIdleMode = 2
            seedIdleLimit = 0
        try:
            self.trc.change_torrent(ids=hash_string,
                                    uploadLimited=uploadLimited,
                                    uploadLimit=uploadLimit,
                                    downloadLimited=downloadLimited,
                                    downloadLimit=downloadLimit,
                                    seedRatioMode=seedRatioMode,
                                    seedRatioLimit=seedRatioLimit,
                                    seedIdleMode=seedIdleMode,
                                    seedIdleLimit=seedIdleLimit)
            return True
        except Exception as err:
            logger.error(f"设置种子出错：{str(err)}")
            return False

    def update_tracker(self, hash_string: str, tracker_list: list = None) -> bool:
        """
        tr4.0及以上弃用直接设置tracker 共用change方法
        https://github.com/trim21/transmission-rpc/blob/8eb82629492a0eeb0bb565f82c872bf9ccdcb313/transmission_rpc/client.py#L654
        """
        if not self.trc:
            return False
        try:
            self.trc.change_torrent(ids=hash_string,
                                    tracker_list=tracker_list)
            return True
        except Exception as err:
            logger.error(f"修改tracker出错：{str(err)}")
            return False

    def get_session(self) -> Optional[Session]:
        """
        获取Transmission当前的会话信息和配置设置
        :return dict
        """
        if not self.trc:
            return None
        try:
            return self.trc.get_session()
        except Exception as err:
            logger.error(f"获取session出错：{str(err)}")
            return None
