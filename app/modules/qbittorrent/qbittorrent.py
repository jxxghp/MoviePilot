"""
Qbittorrent 客户端连接与下载任务操作封装。
"""

import time
import traceback
from http.cookies import SimpleCookie
from typing import Any, List, Optional, Tuple, Union
from urllib.parse import urlparse

import qbittorrentapi
from packaging.version import InvalidVersion, Version
from qbittorrentapi import TorrentDictionary, TorrentFilesList
from qbittorrentapi.client import Client
from qbittorrentapi.transfer import TransferInfoDictionary

from app.domain import torrent as torrent_rules
from app.foundation import url as url_tools
from app.runtime.log import logger

# 临时标签清理最多确认 10 次，等待新任务进入查询索引。
_TORRENT_TAG_CLEANUP_RETRY_TIMES = 10
# 标签清理确认之间的等待秒数。
_TORRENT_TAG_CLEANUP_RETRY_INTERVAL = 1
# 建连最多等待 3 秒，避免不可达实例长时间阻塞调用。
_DOWNLOADER_CONNECT_TIMEOUT_SECONDS = 3
# 响应读取最多等待 60 秒，兼顾大批量任务查询。
_DOWNLOADER_READ_TIMEOUT_SECONDS = 60


class Qbittorrent:
    """
    Qbittorrent 下载器客户端，封装连接、任务查询和管理操作。
    """

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None,
                 username: Optional[str] = None, password: Optional[str] = None,
                 apikey: Optional[str] = None,
                 category: Optional[bool] = False, sequentail: Optional[bool] = False,
                 force_resume: Optional[bool] = False, first_last_piece: bool = False,
                 incomplete_files_ext: Optional[bool] = True,
                 **kwargs: object) -> None:
        """
        根据显式配置连接 qBittorrent，并同步未完成文件后缀开关。

        :param host: 服务地址；省略端口参数时从地址解析端口，缺少地址时保留未连接状态
        :param port: 服务端口，可随 host 一并提供
        :param username: Web API 登录用户名
        :param password: Web API 登录密码
        :param apikey: API 密钥；非空时使用 Bearer 认证代替用户名密码登录
        :param category: 是否启用自动种子管理
        :param sequentail: 是否启用顺序下载，参数名保留既有拼写
        :param force_resume: 是否允许强制启动下载和做种
        :param first_last_piece: 是否优先下载首尾分块
        :param incomplete_files_ext: 是否给未完成文件追加 .!qB 后缀，会同步至下载器全局设置
        :param kwargs: 为通用服务配置保留的额外参数，当前不读取
        """
        self.qbc: Optional[Client] = None
        self._host: Optional[str] = None
        self._port: Optional[int] = None
        self._force_resume = force_resume
        if host and port:
            self._host, self._port = host, port
        elif host:
            self._host, self._port = url_tools.parse_address(address=host, include_scheme=True)
        else:
            logger.error("Qbittorrent配置不完整！")
            return
        self._username = username
        self._password = password
        self._apikey = str(apikey or "").strip() or None
        self._category = category
        self._sequentail = sequentail
        self._first_last_piece = first_last_piece
        self._incomplete_files_ext = incomplete_files_ext
        self.qbc = self.__login_qbittorrent()

    @staticmethod
    def __get_mapping_value(data: Any, key: str) -> Any:
        """
        兼容字典、SDK 映射对象与属性对象的字段读取。
        """
        if data is None:
            return None
        if isinstance(data, dict):
            return data.get(key)
        getter = getattr(data, "get", None)
        if callable(getter):
            try:
                return getter(key)
            except Exception:
                pass
        return getattr(data, key, None)

    def __normalize_cookie(self, cookie: Any) -> dict[str, object]:
        """
        投影 Cookie API 支持的非空字段，统一不同 SDK 返回形状。
        """
        result = {}
        for key in ("domain", "path", "name", "value", "expirationDate"):
            value = self.__get_mapping_value(cookie, key)
            if value not in (None, ""):
                result[key] = value
        return result

    @staticmethod
    def __cookie_key(cookie: dict[str, object]) -> Optional[tuple[object, object, object]]:
        """
        以域、路径和名称标识 Cookie；缺少必要身份时返回空。
        """
        name = cookie.get("name")
        domain = cookie.get("domain")
        path = cookie.get("path") or "/"
        if not name or not domain:
            return None
        return domain, path, name

    @staticmethod
    def __build_site_cookies(url: str, cookie_header: str) -> List[dict[str, object]]:
        """
        把站点 Cookie 请求头转换为绑定目标主机的 Cookie API 条目。
        """
        domain = urlparse(url).hostname
        if not domain:
            return []

        raw_cookies = SimpleCookie()
        raw_cookies.load(cookie_header)
        return [
            {
                "domain": domain,
                "path": "/",
                "name": morsel.key,
                "value": morsel.value,
            }
            for morsel in raw_cookies.values()
        ]

    def __parse_add_torrent_response(self, response: Any) -> Tuple[bool, List[str]]:
        """
        兼容旧版 Ok 文本和新版含成功、待处理计数及任务 ID 的添加响应。
        """
        if not response:
            return False, []
        if isinstance(response, str):
            return "Ok" in response, []

        success_count = self.__get_mapping_value(response, "success_count") or 0
        pending_count = self.__get_mapping_value(response, "pending_count") or 0
        added_torrent_ids = self.__get_mapping_value(response, "added_torrent_ids") or []
        if not isinstance(added_torrent_ids, list):
            added_torrent_ids = list(added_torrent_ids)
        added_torrent_ids = [str(torrent_id) for torrent_id in added_torrent_ids if torrent_id]
        if added_torrent_ids:
            return True, added_torrent_ids
        if success_count or pending_count:
            return True, []
        return "Ok" in str(response), []

    def __use_api_key_auth(self) -> bool:
        """
        判断当前实例是否配置 API Key 认证。
        """
        return bool(self._apikey)

    def __supports_cookie_api(self) -> bool:
        """
        检查 Web API 版本是否支持 Cookie 管理，查询失败按不支持处理。
        """
        if not self.qbc:
            return False
        try:
            web_api_version = self.qbc.app_web_api_version()
            supported: bool = Version(str(web_api_version)) >= Version("2.11.3")
            return supported
        except (InvalidVersion, TypeError, ValueError):
            return False
        except Exception as err:
            logger.warn(f"获取 qbittorrent Web API 版本失败，跳过 Cookie API 兼容：{err}")
            return False

    def __sync_download_cookies(self, url: str, cookie_header: str) -> bool:
        """
        保留已有 Cookie 并按域、路径和名称合并当前下载站点的凭据。
        """
        if not self.qbc or not url or not cookie_header or not self.__supports_cookie_api():
            return False

        try:
            site_cookies = self.__build_site_cookies(url=url, cookie_header=cookie_header)
            if not site_cookies:
                return False

            merged_cookies = {}
            for cookie in self.qbc.app_cookies() or []:
                normalized = self.__normalize_cookie(cookie)
                cookie_key = self.__cookie_key(normalized)
                if cookie_key:
                    merged_cookies[cookie_key] = normalized

            for cookie in site_cookies:
                cookie_key = self.__cookie_key(cookie)
                if cookie_key:
                    merged_cookies[cookie_key] = cookie

            self.qbc.app_set_cookies(cookies=list(merged_cookies.values()))
            return True
        except Exception as err:
            logger.error(f"同步下载Cookie出错：{str(err)}")
            return False

    @staticmethod
    def __sync_incomplete_file_suffix(qbt: Client, enabled: bool) -> None:
        """
        同步未完成文件后缀开关，避免监控流程提前整理仍在下载的媒体文件。
        """
        try:
            preferences = qbt.app_preferences() or {}
            if isinstance(preferences, dict) and preferences.get("incomplete_files_ext") is enabled:
                return
            qbt.app_set_preferences({"incomplete_files_ext": enabled})
            action = "开启" if enabled else "关闭"
            logger.info(f"已{action} qbittorrent 未完成文件追加 .!qB 后缀")
        except Exception as err:
            logger.warning(f"同步 qbittorrent 未完成文件后缀失败：{str(err)}")

    def is_inactive(self) -> bool:
        """
        判断已配置的下载器是否需要重新连接。

        :return: 连接配置存在但客户端未建立时返回 True；缺少配置时返回 False
        """
        if not self._host or not self._port:
            return False
        return True if not self.qbc else False

    def reconnect(self) -> None:
        """
        按当前连接配置重建客户端；失败时保留未连接状态。
        """
        self.qbc = self.__login_qbittorrent()

    def __login_qbittorrent(self) -> Optional[Client]:
        """
        连接 qBittorrent，使用短连接超时并关闭 HTTP 自动重试。

        采用地址中显式指定的协议，成功后同步未完成文件后缀配置。

        :return: 登录成功的客户端；配置不完整、认证失败或连接异常时返回 None
        """
        if not self._host or not self._port:
            return None
        try:
            logger.info(f"正在连接 qbittorrent：{self._host}:{self._port}")
            qbt = qbittorrentapi.Client(host=self._host,
                                        port=self._port,
                                        username=self._username,
                                        password=self._password,
                                        EXTRA_HEADERS={"Authorization": f"Bearer {self._apikey}"}
                                        if self.__use_api_key_auth() else None,
                                        VERIFY_WEBUI_CERTIFICATE=False,
                                        REQUESTS_ARGS={
                                            "timeout": (
                                                _DOWNLOADER_CONNECT_TIMEOUT_SECONDS,
                                                _DOWNLOADER_READ_TIMEOUT_SECONDS,
                                            )
                                        },
                                        HTTPADAPTER_ARGS={"max_retries": 0},
                                        FORCE_SCHEME_FROM_HOST=True)
            try:
                if self.__use_api_key_auth():
                    qbt.app_version()
                else:
                    qbt.auth_log_in()
            except Exception as e:
                if e.__class__.__name__ in {"LoginFailed", "Forbidden403Error", "Unauthorized401Error"}:
                    error_hint = "请检查 API Key 是否正确" if self.__use_api_key_auth() else "请检查用户名和密码是否正确"
                    logger.error(f"qbittorrent 登录失败：{str(e).strip() or error_hint}")
                    return None
                stack_trace = "".join(traceback.format_exception(None, e, e.__traceback__))[:2000]
                logger.error(f"qbittorrent 登录失败：{str(e)}\n{stack_trace}")
                return None
            self.__sync_incomplete_file_suffix(qbt, enabled=bool(self._incomplete_files_ext))
            return qbt
        except Exception as err:
            logger.error(f"qbittorrent 连接出错：{str(err)}")
            return None

    def get_torrents(self, ids: Optional[Union[str, list[str]]] = None,
                     status: Optional[str] = None,
                     tags: Optional[Union[str, list[str]]] = None) -> Tuple[List[TorrentDictionary], bool]:
        """
        查询种子任务并按标识和标签筛选。

        :param ids: 种子哈希或哈希列表，省略时查询全部任务
        :param status: 下载器原生状态筛选条件，省略时不限状态
        :param tags: 必须同时包含的标签；字符串按逗号分隔，省略时不按标签筛选
        :return: 任务列表和错误标志；未连接或查询失败时返回空列表及 True，查询无匹配项时错误标志为 False
        """
        if not self.qbc:
            return [], True
        try:
            torrents = self.qbc.torrents_info(torrent_hashes=ids,
                                              status_filter=status)
            if tags:
                results = []
                if not isinstance(tags, list):
                    tags = tags.split(',')
                try:
                    for torrent in torrents:
                        torrent_tags = [str(tag).strip() for tag in torrent.get("tags").split(',')]
                        if set(tags).issubset(set(torrent_tags)):
                            results.append(torrent)
                finally:
                    torrents.clear()
                    del torrents
                return results, False
            return torrents or [], False
        except Exception as err:
            logger.error(f"获取种子列表出错：{str(err)}")
            return [], True

    def get_completed_torrents(self, ids: Optional[Union[str, list[str]]] = None,
                               tags: Optional[Union[str, list[str]]] = None) -> Optional[List[TorrentDictionary]]:
        """
        获取已完成的种子任务。

        仅保留进度完整、剩余字节为零且不处于校验、移动或错误等不可整理状态的任务。

        :param ids: 种子哈希或哈希列表，省略时不限任务
        :param tags: 必须同时包含的标签；字符串按逗号分隔，省略时不按标签筛选
        :return: 匹配的任务列表；未连接或查询失败时返回 None，无匹配项时返回空列表
        """
        if not self.qbc:
            return None
        torrents, error = self.get_torrents(status="completed", ids=ids, tags=tags)
        if error:
            return None
        ret_torrents = []
        for torrent in torrents or []:
            state = str(torrent.get("state") or "").strip().lower()
            progress = torrent.get("progress") or 0
            amount_left = torrent.get("amount_left") or 0
            if (
                    progress >= 1
                    and amount_left <= 0
                    and state not in {
                        "allocating",
                        "checkingdl",
                        "checkingup",
                        "downloading",
                        "error",
                        "forceddl",
                        "missingfiles",
                        "metadl",
                        "moving",
                        "queueddl",
                        "stalleddl",
                        "unknown",
                    }
            ):
                ret_torrents.append(torrent)
        return ret_torrents

    def get_downloading_torrents(self, ids: Optional[Union[str, list[str]]] = None,
                                 tags: Optional[Union[str, list[str]]] = None) -> Optional[List[TorrentDictionary]]:
        """
        获取下载中或等待下载的种子任务。

        :param ids: 种子哈希或哈希列表，省略时不限任务
        :param tags: 必须同时包含的标签；字符串按逗号分隔，省略时不按标签筛选
        :return: 匹配的任务列表；未连接或查询失败时返回 None，无匹配项时返回空列表
        """
        if not self.qbc:
            return None
        torrents, error = self.get_torrents(ids=ids,
                                            status="downloading",
                                            tags=tags)
        return None if error else torrents or []

    def delete_torrents_tag(self, ids: Optional[Union[str, list[str]]], tag: Union[str, list[str]]) -> bool:
        """
        从指定种子移除标签，并删除全局标签定义。

        qBittorrent 5.x 添加任务后可能暂时无法按响应中的种子 ID 查询任务，
        移除接口此时会静默成功，因此需读取任务标签确认清理结果。

        :param ids: 种子Hash列表；未知种子Hash时传入None，仅删除全局标签定义
        :param tag: 标签内容
        :return: 是否删除成功
        """
        if not self.qbc:
            return False
        try:
            if ids:
                expected_hashes = (
                    {torrent_hash.strip().lower() for torrent_hash in ids.split('|') if torrent_hash.strip()}
                    if isinstance(ids, str)
                    else {str(torrent_hash).strip().lower() for torrent_hash in ids if torrent_hash}
                )
                expected_tags = (
                    {tag_name.strip() for tag_name in tag.split(',') if tag_name.strip()}
                    if isinstance(tag, str)
                    else {str(tag_name).strip() for tag_name in tag if tag_name}
                )
                for attempt in range(_TORRENT_TAG_CLEANUP_RETRY_TIMES):
                    self.qbc.torrents_remove_tags(torrent_hashes=ids, tags=tag)
                    torrents = self.qbc.torrents_info(torrent_hashes=ids) or []
                    visible_hashes = {
                        str(torrent.get("hash") or "").strip().lower()
                        for torrent in torrents
                    }
                    tags_removed = all(
                        not expected_tags.intersection(
                            {
                                torrent_tag.strip()
                                for torrent_tag in str(torrent.get("tags") or "").split(',')
                                if torrent_tag.strip()
                            }
                        )
                        for torrent in torrents
                    )
                    if expected_hashes.issubset(visible_hashes) and tags_removed:
                        break
                    if attempt < _TORRENT_TAG_CLEANUP_RETRY_TIMES - 1:
                        time.sleep(_TORRENT_TAG_CLEANUP_RETRY_INTERVAL)
                else:
                    self.qbc.torrents_delete_tags(tags=tag)
                    logger.warning(f"种子 {ids} 的临时Tag未能在重试期限内确认删除：{tag}")
                    return False
            self.qbc.torrents_delete_tags(tags=tag)
            return True
        except Exception as err:
            logger.error(f"删除种子Tag出错：{str(err)}")
            return False

    def remove_torrents_tag(self, ids: Union[str, list[str]], tag: Union[str, list[str]]) -> bool:
        """
        从指定任务移除标签，保留全局标签定义。

        :param ids: 种子哈希或哈希列表
        :param tag: 待移除的标签或标签列表
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self.qbc:
            return False
        try:
            self.qbc.torrents_remove_tags(torrent_hashes=ids, tags=tag)
            return True
        except Exception as err:
            logger.error(f"移除种子Tag出错：{str(err)}")
            return False

    def set_torrents_tag(self, ids: Union[str, list[str]], tags: list[str]) -> bool:
        """
        向指定种子追加标签，保留原有标签。

        :param ids: 种子哈希或哈希列表
        :param tags: 待追加的标签列表
        :return: RPC成功完成时为True，未连接或失败时为False
        """
        if not self.qbc:
            return False
        try:
            self.qbc.torrents_add_tags(tags=tags, torrent_hashes=ids)
            return True
        except Exception as err:
            logger.error(f"设置种子Tag出错：{str(err)}")
            return False

    def is_force_resume(self) -> Optional[bool]:
        """
        读取当前实例的强制启动配置。

        :return: 构造时传入的开关值；传入 None 时保留 None
        """
        return self._force_resume

    def torrents_set_force_start(self, ids: Union[str, list[str]]) -> bool:
        """
        请求强制启动指定种子。

        :param ids: 种子哈希或哈希列表
        :return: 已启用强制启动且请求完成时返回 True；开关关闭、未连接或请求失败时返回 False
        """
        if not self.qbc:
            return False
        if not self._force_resume:
            return False
        try:
            self.qbc.torrents_set_force_start(enable=True, torrent_hashes=ids)
            return True
        except Exception as err:
            logger.error(f"设置强制作种出错：{str(err)}")
            return False

    def __get_last_add_torrentid_by_tag(self, tags: Union[str, list[str]],
                                        status: Optional[str] = None) -> Optional[str]:
        """
        按标签查找新添加任务的哈希。

        :param tags: 必须同时包含的标签；字符串按逗号分隔，省略时不按标签筛选
        :param status: 下载器原生任务状态，省略时不限状态
        :return: 首个匹配任务的哈希；未找到或查询失败时返回 None
        """
        try:
            torrents, _ = self.get_torrents(status=status, tags=tags)
        except Exception as err:
            logger.error(f"获取种子列表出错：{str(err)}")
            return None
        if torrents:
            torrent_id: Optional[str] = torrents[0].get("hash")
            return torrent_id
        else:
            return None

    def get_torrent_id_by_tag(self, tags: Union[str, list[str]],
                              status: Optional[str] = None) -> Optional[str]:
        """
        轮询刚添加任务的临时标签，并清理临时标签。

        最多查询 9 次，每次查询前等待 3 秒。未找到任务时仍尝试删除全局临时标签。

        :param tags: 必须同时包含的标签；字符串按逗号分隔，省略时不按标签筛选
        :param status: 下载器原生任务状态，省略时不限状态
        :return: 首个匹配任务的哈希；重试后仍未找到时返回 None
        """
        torrent_id = None
        # 新任务可能尚未完成索引，最多查询 9 次，每次查询前等待 3 秒。
        for i in range(1, 10):
            time.sleep(3)
            torrent_id = self.__get_last_add_torrentid_by_tag(tags=tags,
                                                              status=status)
            if torrent_id is None:
                continue
            else:
                self.delete_torrents_tag(torrent_id, tags)
                break
        if torrent_id is None:
            self.delete_torrents_tag(None, tags)
        return torrent_id

    def add_torrent(self,
                    content: Union[str, bytes],
                    is_paused: Optional[bool] = False,
                    download_dir: Optional[str] = None,
                    tag: Optional[Union[str, list[str]]] = None,
                    category: Optional[str] = None,
                    cookie: Optional[str] = None,
                    **kwargs: object
                    ) -> Tuple[bool, List[str]]:
        """
        添加种子任务，兼容旧版文本响应和新版任务标识响应。

        :param content: 种子 URL、磁力链接或种子文件字节内容
        :param is_paused: 是否以暂停状态添加，便于在启动前选择文件
        :param download_dir: 下载器保存目录，省略时采用下载器默认目录
        :param tag: 添加到任务的标签
        :param category: 任务分类；分类检查开启时须同时启用自动管理才会传给下载器
        :param cookie: 下载种子链接使用的站点 Cookie；支持 Cookie API 时会合并到下载器凭据
        :param kwargs: 其余参数传给 torrents_add；ignore_category_check 默认为 True，关闭时检查分类与自动管理开关
        :return: 添加请求是否成功及返回的任务标识列表；旧版成功响应可能没有标识，失败时返回 False 和空列表
        """
        if not self.qbc or not content:
            return False, []

        if isinstance(content, str):
            urls = content
            torrent_files = None
        else:
            urls = None
            torrent_files = content

        if download_dir:
            save_path = download_dir
        else:
            save_path = None

        if tag:
            tags = tag
        else:
            tags = None

        # 如果忽略分类检查，则直接使用传入的分类值，否则，仅在分类存在且启用了自动管理时才传递参数
        ignore_category_check = kwargs.pop("ignore_category_check", True)
        if ignore_category_check:
            is_auto = self._category
        else:
            if category and self._category:
                is_auto = True
            else:
                is_auto = False
                category = None
        try:
            cookie_to_use = cookie
            if urls and cookie and not torrent_rules.is_magnet_link(urls):
                if self.__sync_download_cookies(url=urls, cookie_header=cookie):
                    cookie_to_use = None

            qbc_ret = self.qbc.torrents_add(urls=urls,
                                            torrent_files=torrent_files,
                                            save_path=save_path,
                                            is_paused=is_paused,
                                            tags=tags,
                                            use_auto_torrent_management=is_auto,
                                            is_sequential_download=self._sequentail,
                                            is_first_last_piece_priority=self._first_last_piece,
                                            cookie=cookie_to_use,
                                            category=category,
                                            **kwargs)
            return self.__parse_add_torrent_response(qbc_ret)
        except Exception as err:
            logger.error(f"添加种子出错：{str(err)}")
            return False, []

    def start_torrents(self, ids: Union[str, list[str]]) -> bool:
        """
        启动指定种子任务。

        :param ids: 种子哈希或哈希列表
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self.qbc:
            return False
        try:
            self.qbc.torrents_resume(torrent_hashes=ids)
            return True
        except Exception as err:
            logger.error(f"启动种子出错：{str(err)}")
            return False

    def stop_torrents(self, ids: Union[str, list[str]]) -> bool:
        """
        暂停指定种子任务。

        :param ids: 种子哈希或哈希列表
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self.qbc:
            return False
        try:
            self.qbc.torrents_pause(torrent_hashes=ids)
            return True
        except Exception as err:
            logger.error(f"暂停种子出错：{str(err)}")
            return False

    def delete_torrents(self, delete_file: Optional[bool], ids: Union[str, list[str]]) -> bool:
        """
        删除指定种子任务，并按选项删除下载文件。

        :param delete_file: 是否同时删除下载文件
        :param ids: 种子哈希或哈希列表
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self.qbc:
            return False
        if not ids:
            return False
        try:
            self.qbc.torrents_delete(delete_files=delete_file, torrent_hashes=ids)
            return True
        except Exception as err:
            logger.error(f"删除种子出错：{str(err)}")
            return False

    def get_files(self, tid: str, retry: int = 1, interval: float = 0) -> Optional[TorrentFilesList]:
        """
        读取任务的文件清单，等待下载器完成文件索引。

        :param tid: 种子哈希
        :param retry: 最多查询次数，最少执行 1 次
        :param interval: 查询间隔，单位秒；0 表示立即重试
        :return: 非空文件清单；未连接或全部尝试均无结果时返回 None
        """
        if not self.qbc:
            return None
        last_error = None
        retry_times = max(retry, 1)
        for index in range(retry_times):
            try:
                torrent_files = self.qbc.torrents_files(torrent_hash=tid)
                if torrent_files:
                    return torrent_files
            except Exception as err:
                last_error = err
            if index < retry_times - 1 and interval:
                time.sleep(interval)
        if last_error:
            logger.error(f"获取种子文件列表出错：{str(last_error)}")
        return None

    def set_files(self, **kwargs: object) -> bool:
        """
        设置任务文件的下载优先级。

        :param kwargs: 需包含 torrent_hash 和非空 file_ids；priority 为 0 时不下载，为 1 时普通下载
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
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
            logger.error(f"设置种子文件状态出错：{str(err)}")
            return False

    def transfer_info(self) -> Optional[TransferInfoDictionary]:
        """
        获取下载器当前传输速度和累计传输量。

        :return: 客户端统计对象；未连接或查询失败时返回 None
        """
        if not self.qbc:
            return None
        try:
            return self.qbc.transfer_info()
        except Exception as err:
            logger.error(f"获取传输信息出错：{str(err)}")
            return None

    def set_speed_limit(self, download_limit: Optional[float] = None, upload_limit: Optional[float] = None) -> bool:
        """
        设置下载器全局下载和上传限速。

        :param download_limit: 下载限速，单位 KB/s；0 表示不限速，None 按 0 处理
        :param upload_limit: 上传限速，单位 KB/s；0 表示不限速，None 按 0 处理
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self.qbc:
            return False
        download_limit = (download_limit or 0) * 1024
        upload_limit = (upload_limit or 0) * 1024
        try:
            self.qbc.transfer.upload_limit = int(upload_limit)
            self.qbc.transfer.download_limit = int(download_limit)
            return True
        except Exception as err:
            logger.error(f"设置速度限制出错：{str(err)}")
            return False

    def get_speed_limit(self) -> Optional[Tuple[float, float]]:
        """
        读取下载器全局下载和上传限速。

        :return: 依次为下载、上传限速，单位 KB/s；未连接时返回 None，读取抛出异常的项保留初始化值 0
        """
        if not self.qbc:
            return None

        download_limit = 0
        upload_limit = 0
        try:
            download_limit = self.qbc.transfer.download_limit
            upload_limit = self.qbc.transfer.upload_limit
        except Exception as err:
            logger.error(f"获取速度限制出错：{str(err)}")

        return download_limit / 1024, upload_limit / 1024

    def recheck_torrents(self, ids: Union[str, list[str]]) -> bool:
        """
        请求重新校验指定任务的下载文件。

        :param ids: 种子哈希或哈希列表
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self.qbc:
            return False
        try:
            self.qbc.torrents_recheck(torrent_hashes=ids)
            return True
        except Exception as err:
            logger.error(f"重新校验种子出错：{str(err)}")
            return False

    def change_torrent(
            self,
            hash_string: str,
            upload_limit: Optional[float] = None,
            download_limit: Optional[float] = None,
            ratio_limit: Optional[float] = None,
            seeding_time_limit: Optional[int] = None,
    ) -> bool:
        """
        修改单个种子的限速和做种策略。

        逐项发送修改请求，后续失败不会回滚已生效的设置。仅修改做种时间时，分享率发送为 -2。

        :param hash_string: 种子哈希
        :param upload_limit: 上传限速，单位 KB/s；0 表示不限速，None 表示不修改
        :param download_limit: 下载限速，单位 KB/s；0 表示不限速，None 表示不修改
        :param ratio_limit: 分享率限制，保留 qBittorrent 特殊值语义；None 表示不修改该项
        :param seeding_time_limit: 做种时间限制，单位分钟；同时设置分享率时，缺省或 0 会发送为 -1
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self.qbc or not hash_string:
            return False
        try:
            if upload_limit is not None:
                self.qbc.torrents_set_upload_limit(
                    limit=int(float(upload_limit) * 1024),
                    torrent_hashes=hash_string,
                )
            if download_limit is not None:
                self.qbc.torrents_set_download_limit(
                    limit=int(float(download_limit) * 1024),
                    torrent_hashes=hash_string,
                )
            if ratio_limit is not None:
                self.qbc.torrents_set_share_limits(
                    ratio_limit=round(float(ratio_limit), 2),
                    seeding_time_limit=int(seeding_time_limit or -1),
                    inactive_seeding_time_limit=-1,
                    torrent_hashes=hash_string,
                )
            elif seeding_time_limit is not None:
                self.qbc.torrents_set_share_limits(
                    ratio_limit=-2,
                    seeding_time_limit=int(seeding_time_limit),
                    inactive_seeding_time_limit=-1,
                    torrent_hashes=hash_string,
                )
            return True
        except Exception as err:
            logger.error(f"设置种子属性出错：{str(err)}")
            return False

    def set_torrent_location(self, hash_string: str, location: str) -> bool:
        """
        修改种子任务的保存目录。

        :param hash_string: 种子哈希
        :param location: 下载器可访问的新目录路径，不接受空值
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self.qbc or not hash_string or not location:
            return False
        try:
            self.qbc.torrents_set_location(location=location, torrent_hashes=hash_string)
            return True
        except Exception as err:
            logger.error(f"设置种子保存目录出错：{str(err)}")
            return False

    def set_torrent_category(self, hash_string: str, category: str) -> bool:
        """
        修改种子分类。

        :param hash_string: 种子Hash
        :param category: 分类名称
        :return: 是否修改成功
        """
        if not self.qbc or not hash_string:
            return False
        try:
            self.qbc.torrents_set_category(category=category or "", torrent_hashes=hash_string)
            return True
        except Exception as err:
            logger.error(f"设置种子分类出错：{str(err)}")
            return False

    def update_tracker(self, hash_string: str, tracker_list: list[str]) -> bool:
        """
        为指定种子追加 Tracker 地址。

        :param hash_string: 种子哈希
        :param tracker_list: 待添加的 Tracker 地址列表
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self.qbc:
            return False
        try:
            self.qbc.torrents_add_trackers(torrent_hash=hash_string, urls=tracker_list)
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
        if not self.qbc or not hash_string:
            return None
        try:
            trackers = self.qbc.torrents_trackers(torrent_hash=hash_string) or []
            return [
                tracker.get("url")
                for tracker in trackers
                if tracker.get("url")
            ]
        except Exception as err:
            logger.error(f"获取tracker出错：{str(err)}")
            return None

    def get_content_layout(self) -> Optional[str]:
        """
        读取下载器的全局种子内容布局。

        :return: Original 表示原始布局，Subfolder 表示创建子目录，NoSubfolder 表示不创建子目录；
            设置缺失时默认 Original，未连接时返回 None
        """
        if not self.qbc:
            return None
        application = self.qbc.application.preferences
        # 获取种子内容布局: `Original: 原始, Subfolder: 创建子文件夹, NoSubfolder: 不创建子文件夹`
        layout: Optional[str] = application.get("torrent_content_layout", "Original")
        return layout
