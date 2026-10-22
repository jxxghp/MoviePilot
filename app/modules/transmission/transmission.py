"""
Transmission 客户端连接与下载任务操作封装。
"""

import time
from typing import List, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

import transmission_rpc
from transmission_rpc import Client, File, Torrent
from transmission_rpc.session import Session, SessionStats

from app.foundation.url import UrlUtils
from app.runtime.log import logger

TorrentIds = Union[str, int, Sequence[Union[str, int]]]

# 每次启停的状态确认窗口与轮询间隔，单位均为秒。
_TORRENT_STATE_TIMEOUT = 5.0
_TORRENT_STATE_POLL_INTERVAL = 0.5
# 启动后允许等待校验、下载或做种队列，不要求已经产生下载流量。
_TORRENT_STARTED_STATES = {
    "check pending", "checking", "download pending", "downloading", "seed pending", "seeding",
}


class Transmission:
    """
    Transmission 下载器客户端，封装连接、任务查询和管理操作。
    """
    # 参考transmission web，仅查询需要的参数，加速种子搜索
    _trarg = ["id", "name", "status", "labels", "hashString", "totalSize", "percentDone", "addedDate", "trackerList",
              "trackerStats",
              "leftUntilDone", "rateDownload", "rateUpload", "recheckProgress", "rateDownload", "rateUpload",
              "peersGettingFromUs", "peersSendingToUs", "uploadRatio", "uploadedEver", "downloadedEver", "downloadDir",
              "error", "errorString", "doneDate", "queuePosition", "activityDate", "trackers",
              "downloadLimit", "uploadLimit", "seedRatioLimit", "seedIdleLimit"]

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None,
                 username: Optional[str] = None, password: Optional[str] = None,
                 rename_partial_files: Optional[bool] = True, **kwargs: object) -> None:
        """
        根据显式配置连接 Transmission，并同步未完成文件后缀开关。

        :param host: 服务主机或完整 URL；缺少地址或解析失败时保留未连接状态
        :param port: 服务端口，省略时从 host 解析
        :param username: RPC 登录用户名
        :param password: RPC 登录密码
        :param rename_partial_files: 是否给未完成文件追加 .part 后缀，会同步到下载器全局设置
        :param kwargs: 同时提供 host 和 port 时，protocol 指定协议，默认 http；其余参数当前不读取
        """
        self.trc: Optional[Client] = None
        self._host: Optional[str] = None
        self._port: Optional[int] = None
        self._protocol: object
        self._path = "/transmission/rpc"
        if host and port:
            self._protocol, self._host, self._port = kwargs.get("protocol", "http"), host, port
        elif host:
            result = UrlUtils.parse_url_params(url=host)
            if result:
                self._protocol, self._host, self._port, _path = result
                self._path = urlsplit(host if "://" in host else f"http://{host}").path or self._path
                if self._path == "/":
                    self._path = "/transmission/rpc"
            else:
                logger.error("Transmission配置不正确！")
                return
        else:
            logger.error("Transmission配置不完整！")
            return
        self._username = username
        self._password = password
        self._rename_partial_files = rename_partial_files
        self.trc = self.__login_transmission()

    @staticmethod
    def __sync_incomplete_file_suffix(trt: Client, enabled: bool) -> None:
        """
        同步未完成文件后缀开关，避免监控流程提前整理仍在下载的媒体文件。
        """
        try:
            session = trt.get_session()
            getter = getattr(session, "get", None)
            if callable(getter):
                rename_partial_files = getter("rename-partial-files")
            else:
                rename_partial_files = getattr(session, "rename_partial_files", None)
            if rename_partial_files is enabled:
                return
            trt.set_session(rename_partial_files=enabled)
            action = "开启" if enabled else "关闭"
            logger.info(f"已{action} transmission 未完成文件追加 .part 后缀")
        except Exception as err:
            logger.warning(f"同步 transmission 未完成文件后缀失败：{str(err)}")

    def __login_transmission(self) -> Optional[Client]:
        """
        连接 Transmission，并分别限制建连和响应读取时间。

        :return: 连接成功的客户端；缺少连接配置或发生异常时返回 None
        """
        if not self._host or not self._port:
            return None
        try:
            logger.info(f"正在连接 transmission：{self._protocol}://{self._host}:{self._port}")
            # 协议值沿用动态服务配置，保留客户端调用处既有的静态检查豁免。
            trt = transmission_rpc.Client(protocol=self._protocol, # noqa
                                          host=self._host,
                                          port=self._port,
                                          path=self._path,
                                          username=self._username,
                                          password=self._password,
                                          # transmission-rpc 运行时接受 Requests 的连接与读取超时二元组。
                                          timeout=(3, 60))
            self.__sync_incomplete_file_suffix(trt, enabled=bool(self._rename_partial_files))
            return trt
        except Exception as err:
            logger.error(f"transmission 连接出错：{str(err)}")
            return None

    def is_inactive(self) -> bool:
        """
        判断已配置的下载器是否需要重新连接。

        :return: 连接配置存在但客户端未建立时返回 True；缺少配置时返回 False
        """
        if not self._host or not self._port:
            return False
        return True if not self.trc else False

    def reconnect(self) -> None:
        """
        按当前连接配置重建客户端；失败时保留未连接状态。
        """
        self.trc = self.__login_transmission()

    def get_torrents(self, ids: Optional[TorrentIds] = None, status: Optional[Union[str, list[str]]] = None,
                     tags: Optional[Union[str, list[str]]] = None) -> Tuple[List[Torrent], bool]:
        """
        查询种子任务并按标识和标签筛选。

        :param ids: 种子哈希或哈希列表，省略时查询全部任务
        :param status: 下载器原生状态筛选条件，省略时不限状态
        :param tags: 必须同时包含的标签；字符串按逗号分隔，省略时不按标签筛选
        :return: 任务列表和错误标志；未连接或查询失败时返回空列表及 True，查询无匹配项时错误标志为 False
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
            tags = tags.split(',')
        ret_torrents = []
        try:
            for torrent in torrents:
                if status and torrent.status not in status:
                    continue
                labels = [str(tag).strip()
                          for tag in torrent.labels] if hasattr(torrent, "labels") else []
                if tags and not set(tags).issubset(set(labels)):
                    continue
                ret_torrents.append(torrent)
        finally:
            torrents.clear()
            del torrents
        return ret_torrents, False

    def get_completed_torrents(self, ids: Optional[TorrentIds] = None,
                               tags: Optional[Union[str, list[str]]] = None) -> Optional[List[Torrent]]:
        """
        获取已完成的种子任务。

        :param ids: 种子哈希或哈希列表，省略时不限任务
        :param tags: 必须同时包含的标签；字符串按逗号分隔，省略时不按标签筛选
        :return: 匹配的任务列表；未连接或查询失败时返回 None，无匹配项时返回空列表
        """
        if not self.trc:
            return None
        try:
            torrents, error = self.get_torrents(status=["seeding", "seed pending"], ids=ids, tags=tags)
            return None if error else torrents or []
        except Exception as err:
            logger.error(f"获取已完成的种子列表出错：{str(err)}")
            return None

    def get_downloading_torrents(self, ids: Optional[TorrentIds] = None,
                                 tags: Optional[Union[str, list[str]]] = None) -> Optional[List[Torrent]]:
        """
        获取下载中或等待下载的种子任务。

        :param ids: 种子哈希或哈希列表，省略时不限任务
        :param tags: 必须同时包含的标签；字符串按逗号分隔，省略时不按标签筛选
        :return: 匹配的任务列表；未连接或查询失败时返回 None，无匹配项时返回空列表
        """
        if not self.trc:
            return None
        try:
            torrents, error = self.get_torrents(ids=ids,
                                                status=["downloading", "download pending"],
                                                tags=tags)
            return None if error else torrents or []
        except Exception as err:
            logger.error(f"获取正在下载的种子列表出错：{str(err)}")
            return None

    def set_torrent_tag(self, ids: TorrentIds, tags: list[str], org_tags: Optional[list[str]] = None) -> bool:
        """
        写入任务标签，默认覆盖原有标签。

        :param ids: 种子哈希或哈希列表
        :param tags: 需要写入的非空标签列表
        :param org_tags: 需要保留的原有标签；与新标签合并去重后统一写入
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
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

    def get_torrent_tags(self, ids: TorrentIds) -> List[str]:
        """
        读取匹配结果中第一个种子的标签。

        :param ids: 种子哈希或哈希列表
        :return: 首个任务的标签列表；未连接、无任务或查询失败时返回空列表
        """
        if not self.trc:
            return []
        try:
            torrents = self.trc.get_torrents(ids=ids, arguments=self._trarg)
            if len(torrents):
                torrent = torrents[0]
                labels = [str(tag).strip()
                          for tag in torrent.labels] if hasattr(torrent, "labels") else []
                return labels
        except Exception as err:
            logger.error(f"获取种子标签出错：{str(err)}")
            return []
        return []

    def add_torrent(self, content: Union[str, bytes],
                    is_paused: Optional[bool] = False,
                    download_dir: Optional[str] = None,
                    labels: Optional[list[str]] = None,
                    cookie: Optional[str] = None) -> Optional[Torrent]:
        """
        向 Transmission 添加下载任务。

        :param content: 种子 URL、磁力链接或文件字节内容
        :param is_paused: 是否以暂停状态添加
        :param download_dir: 下载器保存目录，省略时使用下载器默认值
        :param labels: 添加到任务的标签列表
        :param cookie: 下载种子 URL 使用的站点 Cookie
        :return: 客户端返回的种子对象；未连接或请求失败时返回 None
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

    def start_torrents(self, ids: TorrentIds) -> bool:
        """
        启动指定种子任务。

        :param ids: 种子哈希或哈希列表
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self.trc:
            return False
        try:
            self.trc.start_torrent(ids=ids)
            return True
        except Exception as err:
            logger.error(f"启动种子出错：{str(err)}")
            return False

    def stop_torrents(self, ids: TorrentIds) -> bool:
        """
        暂停指定种子任务。

        :param ids: 种子哈希或哈希列表
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self.trc:
            return False
        try:
            self.trc.stop_torrent(ids=ids)
            return True
        except Exception as err:
            logger.error(f"停止种子出错：{str(err)}")
            return False

    def ensure_torrent_state(self, tid: str, *, paused: bool) -> bool:
        """
        在选集边界确认任务实际暂停或启动，避免异步停止覆盖后续启动。

        已暂停的任务不再发送停止请求。启动后只在仍停止时重试启动，
        校验、下载、做种及对应排队状态均视为已接管，不强制跳过队列。

        :param tid: 需要确认状态的单个种子哈希
        :param paused: 为 True 时等待实际停止，为 False 时请求启动并确认状态
        :return: 状态已确认时返回 True；未连接、RPC 失败或五秒内未确认时返回 False
        """
        if not self.trc:
            return False
        if not paused and not self.start_torrents(tid):
            return False
        expected = {"stopped"} if paused else _TORRENT_STARTED_STATES
        deadline = time.monotonic() + _TORRENT_STATE_TIMEOUT
        stop_requested = False
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                status = self.trc.get_torrent(tid, arguments=["status"], timeout=remaining).status
            except Exception as err:
                logger.error(f"确认种子 {tid} 启停状态出错：{str(err)}")
                return False
            if status in expected:
                return True
            if paused:
                if not stop_requested and not self.stop_torrents(tid):
                    return False
                stop_requested = True
            elif status == "stopped" and not self.start_torrents(tid):
                return False
            time.sleep(min(_TORRENT_STATE_POLL_INTERVAL, max(0, deadline - time.monotonic())))
        action = "暂停" if paused else "启动"
        logger.warning(f"种子 {tid} 在 {_TORRENT_STATE_TIMEOUT} 秒内未确认{action}状态")
        return False

    def delete_torrents(self, delete_file: Optional[bool], ids: TorrentIds) -> bool:
        """
        删除指定种子任务，并按选项删除下载文件。

        :param delete_file: 是否同时删除下载文件
        :param ids: 种子哈希或哈希列表
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
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
        读取种子文件列表，兼容新旧 transmission-rpc 文件接口。

        :param tid: 种子哈希
        :return: 文件列表；哈希为空、任务不存在、未连接或查询失败时返回 None
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
        if not torrent:
            return None
        try:
            get_files = getattr(torrent, "get_files", None)
            if callable(get_files):
                files: List[File] = get_files()
                return files
            legacy_files: List[File] = torrent.files()
            return legacy_files
        except Exception as err:
            logger.error(f"获取种子文件列表出错：{str(err)}")
            return None

    def set_files(self, tid: str, file_ids: list[int]) -> bool:
        """
        将选定文件设为需要下载。

        :param tid: 种子哈希
        :param file_ids: 种子内部的文件索引列表；空列表不修改任何文件
        :return: 已连接且无需修改或请求完成时返回 True；未连接或请求失败时返回 False，
            不代表任务已完成该动作
        """
        if not self.trc:
            return False
        # Transmission 把空数组解释为全部文件，空选择不能直接传入 RPC
        if not file_ids:
            return True
        try:
            self.trc.change_torrent(ids=tid, files_wanted=file_ids)
            return True
        except Exception as err:
            logger.error(f"设置下载文件状态出错：{str(err)}")
            return False

    def set_unwanted_files(self, tid: str, file_ids: list[int]) -> bool:
        """
        将选定文件设为不下载。

        :param tid: 种子哈希
        :param file_ids: 种子内部的文件索引列表；空列表不修改任何文件
        :return: 已连接且无需修改或请求完成时返回 True；未连接或请求失败时返回 False，
            不代表任务已完成该动作
        """
        if not self.trc:
            return False
        # 空的不下载列表表示无需排除文件，不能触发 RPC 的全部文件语义
        if not file_ids:
            return True
        try:
            self.trc.change_torrent(ids=tid, files_unwanted=file_ids)
            return True
        except Exception as err:
            logger.error(f"设置下载文件状态出错：{str(err)}")
            return False

    def transfer_info(self) -> Optional[SessionStats]:
        """
        获取下载器当前传输速度和累计传输量。

        :return: 客户端统计对象；未连接或查询失败时返回 None
        """
        if not self.trc:
            return None
        try:
            return self.trc.session_stats()
        except Exception as err:
            logger.error(f"获取传输信息出错：{str(err)}")
            return None

    def set_speed_limit(self, download_limit: Optional[float] = None, upload_limit: Optional[float] = None) -> bool:
        """
        设置下载器全局下载和上传限速。

        必须同时提供两项限速；缺少任意一项时返回 False 且不修改会话。

        :param download_limit: 下载限速，单位 KB/s；0 表示不限速，不可省略
        :param upload_limit: 上传限速，单位 KB/s；0 表示不限速，不可省略
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self.trc:
            return False
        if download_limit is None or upload_limit is None:
            logger.error("设置速度限制出错：未提供下载或上传限速")
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
        读取下载器全局下载和上传限速。

        :return: 依次为下载、上传限速，单位 KB/s；未连接时返回 None，读取抛出异常的项保留初始化值 0
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

    def recheck_torrents(self, ids: TorrentIds) -> bool:
        """
        请求重新校验指定任务的下载文件。

        :param ids: 种子哈希或哈希列表
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
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
                       upload_limit: Optional[float] = None,
                       download_limit: Optional[float] = None,
                       ratio_limit: Optional[float] = None,
                       seeding_time_limit: Optional[int] = None) -> bool:
        """
        修改单个种子的限速和做种停止条件。

        :param hash_string: 种子哈希
        :param upload_limit: 上传限速，单位 KB/s；0 关闭限速，None 不修改
        :param download_limit: 下载限速，单位 KB/s；0 关闭限速，None 不修改
        :param ratio_limit: 分享率阈值，0 关闭限制，None 不修改
        :param seeding_time_limit: 空闲空闲做种时间限制，单位分钟；0 关闭限制，None 不修改
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not hash_string:
            return False
        if not self.trc:
            logger.error("设置种子出错：Transmission 客户端未连接")
            return False
        change_kwargs: dict[str, object] = {"ids": hash_string}
        if upload_limit is not None:
            change_kwargs["uploadLimited"] = bool(upload_limit)
            change_kwargs["uploadLimit"] = int(upload_limit)
        if download_limit is not None:
            change_kwargs["downloadLimited"] = bool(download_limit)
            change_kwargs["downloadLimit"] = int(download_limit)
        if ratio_limit is not None:
            change_kwargs["seedRatioMode"] = 1 if ratio_limit else 2
            change_kwargs["seedRatioLimit"] = round(float(ratio_limit), 2) if ratio_limit else 0
        if seeding_time_limit is not None:
            change_kwargs["seedIdleMode"] = 1 if seeding_time_limit else 2
            change_kwargs["seedIdleLimit"] = int(seeding_time_limit) if seeding_time_limit else 0
        try:
            self.trc.change_torrent(**change_kwargs)
            return True
        except Exception as err:
            logger.error(f"设置种子出错：{str(err)}")
            return False

    def set_torrent_location(self, hash_string: str, location: str) -> bool:
        """
        修改种子任务的保存目录。

        优先使用客户端的数据移动接口；旧客户端退回修改下载目录配置。

        :param hash_string: 种子哈希
        :param location: 下载器可访问的新目录路径，不接受空值
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self.trc or not hash_string or not location:
            return False
        try:
            move_torrent_data = getattr(self.trc, "move_torrent_data", None)
            if callable(move_torrent_data):
                move_torrent_data(ids=hash_string, location=location)
            else:
                self.trc.change_torrent(ids=hash_string, download_dir=location)
            return True
        except Exception as err:
            logger.error(f"设置种子保存目录出错：{str(err)}")
            return False

    def update_tracker(self, hash_string: str, tracker_list: Optional[list[str]] = None) -> bool:
        """
        通过任务修改接口设置 Tracker 列表。

        Transmission 4.0 及以上统一使用 change_torrent 接口修改 Tracker。
        上游接口参考：
        https://github.com/trim21/transmission-rpc/blob/8eb82629492a0eeb0bb565f82c872bf9ccdcb313/transmission_rpc/client.py#L654

        :param hash_string: 种子哈希
        :param tracker_list: 要写入的 Tracker 地址列表，每个地址作为独立层级交给客户端
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self.trc:
            return False
        try:
            self.trc.change_torrent(ids=hash_string,
                                    tracker_list=[[url] for url in tracker_list] if tracker_list is not None else None)
            return True
        except Exception as err:
            logger.error(f"修改tracker出错：{str(err)}")
            return False

    def get_trackers(self, hash_string: str) -> Optional[List[str]]:
        """
        查询种子任务的 Tracker 地址列表。

        :param hash_string: 种子哈希
        :return: 地址列表；任务无 Tracker 时为空列表，未连接、哈希为空或请求失败时返回 None
        """
        if not self.trc or not hash_string:
            return None
        try:
            torrents = self.trc.get_torrents(ids=hash_string, arguments=self._trarg)
            if not torrents:
                return []
            torrent = torrents[0]
            tracker_list = getattr(torrent, "tracker_list", None) \
                or getattr(torrent, "trackerList", None) \
                or []
            if tracker_list:
                return list(tracker_list)
            trackers = getattr(torrent, "trackers", None) or []
            return [
                announce
                for tracker in trackers
                if isinstance(tracker, dict) and (announce := tracker.get("announce"))
            ]
        except Exception as err:
            logger.error(f"获取tracker出错：{str(err)}")
            return None

    def get_session(self) -> Optional[Session]:
        """
        读取 Transmission 当前会话和全局配置。

        :return: 客户端会话对象；未连接或查询失败时返回 None
        """
        if not self.trc:
            return None
        try:
            return self.trc.get_session()
        except Exception as err:
            logger.error(f"获取session出错：{str(err)}")
            return None
