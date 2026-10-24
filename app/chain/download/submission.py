"""
种子获取、提交前校验与单个下载任务提交。
"""

import base64
import hashlib
import json
import re
from copy import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set, Tuple, Union, cast
from urllib.parse import urlencode, urljoin, urlparse

from app.application.configuration import get_chain_runtime_config_snapshot
from app.application.directory import (
    specified_download_save_path,
    validate_download_save_path,
)
from app.application.download.admission import SubscriptionDownloadGovernance, subscription_download_label

# 站点资源包在启动时加载，静态检查环境可能无法解析其实现。
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.application.torrent.download import TorrentHelper
from app.chain.download.contract import _DownloadOwnerBase
from app.chain.download.ports import (
    _close_download_response,
    _download_ports_snapshot,
)
from app.chain.media import MediaChain
from app.domain import episode as episode_rules
from app.domain import torrent as torrent_rules
from app.domain.context import (
    Context,
    MediaInfo,
    MusicInfo,
    TorrentInfo,
)
from app.domain.meta.metabase import MetaBase
from app.runtime.cache import FileCache
from app.runtime.events import eventmanager
from app.runtime.log import logger
from app.schemas.download import DOWNLOAD_TASK_ALREADY_EXISTS
from app.schemas.event import ResourceDownloadEventData
from app.schemas.file import FileURI
from app.schemas.media import resolve_media_identity
from app.schemas.message import Message
from app.schemas.types import (
    ChainEventType,
    MediaType,
    MessageType,
    NotificationChannel,
)


@dataclass(frozen=True, slots=True)
class _PreparedDownload:
    """
    下载器调用前已经验证并规范化的本地提交事实。

    :param torrent: 已补齐来源凭据并规范化地址的种子信息
    :param media: 已识别的影视或音乐目标信息
    :param meta: 下载资源自身的标题解析元数据
    :param torrent_content: 可交给下载器的磁力链接、下载地址或种子字节内容
    :param folder_name: 种子内容对应的顶层目录名称
    :param file_list: 种子中待处理文件的相对路径列表
    :param download_dir: 最终下载保存 URI 的 Path 表示，保留既有下载器调用格式
    :param download_uri: 按目录配置解析出的最终下载保存地址
    :param download_episodes: 本次下载的集数范围文本，非剧集或无需限制时为 None
    :param site_downloader: 站点配置指定的下载器名称，未指定时为 None
    """

    torrent: TorrentInfo
    media: Union[MediaInfo, MusicInfo]
    meta: MetaBase
    torrent_content: Union[str, bytes]
    folder_name: str
    file_list: list[str]
    download_dir: Path
    download_uri: str
    download_episodes: Optional[str]
    site_downloader: Optional[str]


class _DownloadResourceOwner(_DownloadOwnerBase):
    """
    种子获取、间接地址解析与资源下载事件处理。
    """

    @staticmethod
    def _credential_origin(url: Optional[str]) -> Optional[tuple[str, str, int]]:
        """
        仅返回无内嵌账号的 HTTP 来源，避免按宽泛域名向其他地址补发凭据。
        """
        try:
            parts = urlparse(url or "")
            if (parts.scheme not in ("http", "https") or not parts.hostname
                    or parts.username is not None or parts.password is not None):
                return None
            port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
            return parts.scheme, parts.hostname, port
        except ValueError:
            return None

    def _with_site_credentials(self, torrent: TorrentInfo) -> TorrentInfo:
        """
        为同来源的直接下载补齐 Cookie 和缺失 UA，不改写调用方对象或换票协议。
        """
        if torrent.site_cookie:
            return torrent
        origin = self._credential_origin(torrent.enclosure)
        if origin is None:
            return torrent
        try:
            if torrent.site is not None:
                site = self.site_repository.get(torrent.site)
            else:
                matches = [
                    item for item in self.site_repository.list()
                    if self._credential_origin(item.url) == origin
                ]
                site = matches[0] if len(matches) == 1 else None
        except Exception as err:
            # 查询异常可能包含数据库连接信息，只记录类型并保留既有下载行为。
            logger.warning(f"读取种子下载站点凭据失败：{type(err).__name__}")
            return torrent
        if not site or not site.cookie or self._credential_origin(site.url) != origin:
            return torrent
        enriched = copy(torrent)
        enriched.site_cookie = site.cookie
        if not torrent.site_ua and site.ua:
            enriched.site_ua = site.ua
        return enriched

    @staticmethod
    def _normalize_indirect_download_url(url: str, base_url: Optional[str] = None) -> str:
        """
        将两段式下载结果约束到索引器配置的可信 API 地址。

        :param url: 换票接口返回的临时下载地址
        :param base_url: 索引器配置的可信 API Base URL
        :return: 使用可信 API 来源的临时下载地址
        """
        if not url or not base_url:
            return url
        base_parts = urlparse(base_url)
        if not base_parts.scheme or not base_parts.netloc:
            return url
        url_parts = urlparse(url)
        if not url_parts.netloc:
            return urljoin(f"{base_url.rstrip('/')}/", url)
        return url_parts._replace(
            scheme=base_parts.scheme,
            netloc=base_parts.netloc,
        ).geturl()

    def _resolve_indirect_download_url(
            self,
            url: str,
            ua: Optional[str] = None,
            cookie: Optional[str] = None,
    ) -> Optional[str]:
        """
        解析索引器编码的换票请求，并返回可信来源的临时下载地址。
        """
        match = re.search(r"\[(.*)](.*)", url)
        if not match:
            return None
        encoded_params, request_url = match.groups()
        if not encoded_params:
            return request_url
        request_text = base64.b64decode(encoded_params.encode("utf-8")).decode("utf-8")
        request_params = cast(Dict[str, Any], json.loads(request_text))
        if not request_params.get("cookie"):
            cookie = None
        headers_value = request_params.get("header")
        headers = (
            cast(dict[str, str], headers_value)
            if isinstance(headers_value, dict)
            else None
        )
        params_value = request_params.get("params")
        params = (
            cast(dict[str, Any], params_value)
            if isinstance(params_value, dict)
            else None
        )
        http, _ = _download_ports_snapshot()
        proxies = (
            get_chain_runtime_config_snapshot().proxy
            if request_params.get("proxy")
            else None
        )
        if request_params.get("method") == "get":
            response = http.get(
                request_url,
                ua=ua,
                cookies=cookie,
                headers=headers,
                proxies=proxies,
                params=params,
            )
        else:
            response = http.post(
                request_url,
                ua=ua,
                cookies=cookie,
                headers=headers,
                proxies=proxies,
                params=params,
            )
        if not response:
            return None
        try:
            if not request_params.get("result"):
                return response.text
            data: Any = response.json()
            success_key = request_params.get("success")
            if success_key and not data.get(success_key):
                return None
            for key in str(request_params.get("result")).split("."):
                if not isinstance(data, dict):
                    return None
                data = data.get(key)
                if not data:
                    return None
            result_path = request_params.get("result_path")
            result_query_param = request_params.get("result_query_param")
            if result_path and result_query_param:
                if not isinstance(result_query_param, str):
                    return None
                result_url = urljoin(
                    f"{str(request_params.get('result_base_url')).rstrip('/')}/",
                    str(result_path).lstrip("/"),
                )
                return f"{result_url}?{urlencode({result_query_param: data})}"
            if not isinstance(data, str):
                return None
            base_url = request_params.get("result_base_url")
            normalized_url = self._normalize_indirect_download_url(
                url=data,
                base_url=base_url if isinstance(base_url, str) else None,
            )
            logger.info("已获取到站点临时下载地址")
            return normalized_url
        finally:
            _close_download_response(response)

    def download_torrent(self, torrent: TorrentInfo,
                         channel: Optional[NotificationChannel] = None,
                         source: Optional[str] = None,
                         userid: Union[str, int, None] = None,
                         on_failure: Optional[Callable[[str], None]] = None,
                         ) -> Tuple[Optional[Union[str, bytes]], str, list[str]]:
        """
        下载种子文件，如果是磁力链，会返回磁力链接本身
        换票种子按站点资源和原始换票凭据复用本地内容，命中后不再请求临时链接。

        :param on_failure: 将本次具体失败原因交还准备流程，保持返回元组与并发调用隔离
        :return: 种子内容，种子目录名，种子文件清单
        """
        if not torrent.enclosure:
            return None, "", []
        if torrent.enclosure.startswith("magnet:"):
            return torrent.enclosure, "", []
        helper: TorrentHelper = cast(Any, TorrentHelper)()
        if helper.is_invalid(torrent.enclosure):
            if on_failure:
                on_failure("种子已加入下载失败黑名单（24小时）")
            logger.info(f"{torrent.title} 命中下载失败黑名单，跳过下载")
            return None, "", []
        torrent = self._with_site_credentials(torrent)
        result = self._get_torrent_content(torrent)
        if result is None:
            return None, "", []
        content, download_folder, files, error_msg = result

        if isinstance(content, str):
            return content, "", []

        if not content:
            if on_failure:
                on_failure(error_msg or "下载种子内容为空")
            if error_msg and "种子数据有误" in error_msg:
                helper.add_invalid(torrent.enclosure or "")
            logger.error(f"下载种子文件失败：{torrent.title}")
            self.post_message(Message(
                channel=channel,
                source=source if channel else None,
                mtype=MessageType.Manual,
                title=f"{torrent.title} 种子下载失败！",
                text=f"错误信息：{error_msg}\n站点：{torrent.site_name}",
                userid=userid))
            return None, "", []

        return content, download_folder or "", files or []

    @staticmethod
    def _indirect_torrent_cache_key(torrent: TorrentInfo) -> Optional[str]:
        """
        以资源身份和换票凭据隔离缓存，临时链接不参与键，凭据不写入文件名。
        """
        identity = torrent_rules.resource_identity(torrent)
        if not torrent.enclosure or not torrent.enclosure.startswith("[") or identity is None:
            return None
        scope = json.dumps((identity, torrent.enclosure, torrent.site_cookie))
        return f"resource-{hashlib.sha256(scope.encode('utf-8')).hexdigest()}.torrent"

    def _get_torrent_content(self, torrent: TorrentInfo) -> Optional[Tuple[
        Optional[Union[str, bytes]], Optional[str], Optional[list[str]], Optional[str],
    ]]:
        """
        先读取已验证的换票种子内容；缓存损坏时重新换票，失败与磁力链不缓存。
        """
        if not torrent.enclosure:
            return None
        cache_key = self._indirect_torrent_cache_key(torrent)
        cache = FileCache(local_only=True) if cache_key else None
        if cache is not None and cache_key is not None:
            cached = cache.get(cache_key, region="torrents")
            if isinstance(cached, bytes) and cached:
                helper: TorrentHelper = cast(Any, TorrentHelper)()
                cached_folder, cached_files = helper.get_fileinfo_from_torrent_content(cached)
                if cached_files:
                    return cached, cached_folder, cached_files, ""
                cache.delete(cache_key, region="torrents")
        site_cookie: Optional[str] = torrent.site_cookie
        indirect_download = torrent.enclosure.startswith("[")
        if indirect_download:
            torrent_url = self._resolve_indirect_download_url(
                url=torrent.enclosure,
                ua=torrent.site_ua,
                cookie=site_cookie,
            )
            # 涉及解析地址的不使用Cookie下载种子，否则MT会出错
            site_cookie = None
        else:
            torrent_url = torrent.enclosure
        if not torrent_url:
            logger.error(f"{torrent.title} 无法获取下载地址！")
            return None
        _, content, download_folder, files, error_msg = self._download_torrent_content(
            torrent, torrent_url, site_cookie, indirect_download
        )

        if cache is not None and cache_key is not None and isinstance(content, bytes) and files:
            cache.set(cache_key, content, region="torrents")
        return content, download_folder, files, error_msg

    def _download_torrent_content(
            self,
            torrent: TorrentInfo,
            url: str,
            cookie: Optional[str],
            indirect_download: bool,
    ) -> Tuple[Optional[Path], Optional[Union[str, bytes]], Optional[str], Optional[list[str]], Optional[str]]:
        """
        短时签名失效时刷新同一资源并重试一次，最终失败再交由调用方冷却。
        """
        helper: TorrentHelper = cast(Any, TorrentHelper)()
        for attempt in range(2):
            expiring = torrent_rules.is_expiring_download_url(url)
            result = helper.download_torrent(
                url=url,
                cookie=cookie,
                ua=torrent.site_ua or self.runtime_config.user_agent,
                proxy=torrent.site_proxy,
                cache_invalid=not (indirect_download or expiring),
            )
            if result[1] or attempt or indirect_download or not expiring:
                return result
            if not re.fullmatch(r"下载种子出错，状态码：(401|403|404|410)", result[4] or ""):
                return result
            if not self._refresh_signed_torrent(torrent):
                return result
            if not torrent.enclosure:
                return result
            url, cookie = torrent.enclosure, torrent.site_cookie
        return result

    def _refresh_signed_torrent(self, torrent: TorrentInfo) -> bool:
        """
        复用站点索引器查找同站同 ID 的新地址，不允许用同标题的其他资源替换。
        """
        identity = torrent_rules.resource_identity(torrent)
        if not torrent.site or not torrent.title or identity is None:
            return False
        try:
            site = next((item for item in SitesHelper().get_indexers() if item.get("id") == torrent.site), None)
            if not site:
                return False
            for candidate in self.search_site_torrents(site=site, keyword=torrent.title):
                if (torrent_rules.resource_identity(candidate) != identity
                        or not candidate.enclosure
                        or candidate.enclosure == torrent.enclosure
                        or not candidate.enclosure.startswith(("http://", "https://"))):
                    continue
                for name in ("enclosure", "page_url", "site_cookie", "site_ua", "site_proxy"):
                    setattr(torrent, name, getattr(candidate, name))
                logger.info(f"{torrent.title} 已刷新临时下载链接，重试下载")
                return True
        except Exception as err:
            # 索引器异常可能携带签名 URL，不把异常正文写入日志或通知。
            logger.warning(f"{torrent.title} 刷新临时下载链接失败：{type(err).__name__}")
        return False

    @staticmethod
    def _apply_resource_download_event(
            context: Context,
            episodes: Optional[Set[int]],
            channel: Optional[NotificationChannel],
            source: Optional[str],
            downloader: Optional[str],
            save_path: Optional[str],
            userid: Union[str, int, None],
            username: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        """
        应用资源下载事件覆盖，并校验事件返回的下载目录。
        """
        # 空白保存目录表示使用默认下载目录，事件订阅方也只会看到 None。
        save_path = specified_download_save_path(save_path)
        meta = context.meta_info
        media = context.media_info
        event_data = ResourceDownloadEventData(
            context=context,
            episodes=set(episodes or (meta.episode_list if meta else [])),
            channel=channel,
            origin=source,
            downloader=downloader,
            options={
                "save_path": save_path,
                "userid": userid,
                "username": username,
                "media_category": media.category if media else None,
            },
        )
        event = eventmanager.send_event(ChainEventType.ResourceDownload, event_data)
        if event and event.event_data:
            event_data = cast(ResourceDownloadEventData, event.event_data)
            if event_data.cancel:
                logger.debug(
                    "Resource download canceled by event: %s,Reason: %s",
                    event_data.source,
                    event_data.reason,
                )
                return save_path, "下载被事件取消"
            if event_data.options and "save_path" in event_data.options:
                save_path = specified_download_save_path(
                    cast(Optional[str], event_data.options.get("save_path"))
                )
        if save_path is None:
            return None, None
        try:
            return validate_download_save_path(save_path), None
        except ValueError as err:
            logger.warn(str(err))
            return save_path, str(err)


class DownloadSubmissionOwner(_DownloadResourceOwner):
    """
    单任务下载准备、提交与结算 owner。
    """

    @staticmethod
    def _subscription_download_cancelled(
        governance: Optional[SubscriptionDownloadGovernance],
    ) -> bool:
        """
        在下载器副作用前读取订阅执行上下文的停止信号。
        """
        return bool(governance and governance.cancelled and governance.cancelled())

    def download_single(self, context: Context,
                        torrent_file: Optional[Path] = None,
                        torrent_content: Optional[Union[str, bytes]] = None,
                        episodes: Optional[Set[int]] = None,
                        channel: Optional[NotificationChannel] = None,
                        source: Optional[str] = None,
                        downloader: Optional[str] = None,
                        save_path: Optional[str] = None,
                        userid: Union[str, int, None] = None,
                        username: Optional[str] = None,
                        label: Optional[str] = None,
                        return_detail: bool = False,
                        custom_words: Optional[str] = None,
                        governance: Optional[SubscriptionDownloadGovernance] = None,
                        ) -> Union[Optional[str], Tuple[Optional[str], Optional[str]]]:
        """
        下载单个资源并发送结果通知。

        保持下载链、消息入口和插件使用的公开签名，实际流程委托给内部执行阶段。
        """
        return self._execute_download_single(
            context=context,
            torrent_file=torrent_file,
            torrent_content=torrent_content,
            episodes=episodes,
            channel=channel,
            source=source,
            downloader=downloader,
            save_path=save_path,
            userid=userid,
            username=username,
            label=label,
            return_detail=return_detail,
            custom_words=custom_words,
            governance=governance,
        )

    def _execute_download_single(
        self,
        context: Context,
        torrent_file: Optional[Path] = None,
        torrent_content: Optional[Union[str, bytes]] = None,
        episodes: Optional[Set[int]] = None,
        channel: Optional[NotificationChannel] = None,
        source: Optional[str] = None,
        downloader: Optional[str] = None,
        save_path: Optional[str] = None,
        userid: Union[str, int, None] = None,
        username: Optional[str] = None,
        label: Optional[str] = None,
        return_detail: bool = False,
        custom_words: Optional[str] = None,
        governance: Optional[SubscriptionDownloadGovernance] = None,
    ) -> Union[Optional[str], Tuple[Optional[str], Optional[str]]]:
        """
        准备下载事实，提交下载器并按订阅治理合同结算结果。
        """
        prepared, error_msg = self._prepare_download_single(
            context=context,
            torrent_file=torrent_file,
            torrent_content=torrent_content,
            episodes=episodes,
            channel=channel,
            source=source,
            downloader=downloader,
            save_path=save_path,
            userid=userid,
            username=username,
        )
        if prepared is None:
            if governance:
                governance.report(context, "rejected", "资源准备失败或未满足下载提交条件")
            return (None, error_msg) if return_detail else None
        if self._has_completed_music_download(prepared, source):
            reason = "音乐资源的全部音频已成功整理，跳过重复提交"
            logger.info(f"{prepared.torrent.title} {reason}")
            if governance:
                governance.report(context, "rejected", reason)
            return (None, reason) if return_detail else None
        download_hash, error_msg = self._submit_prepared_download(
            prepared=prepared,
            context=context,
            episodes=episodes,
            channel=channel,
            source=source,
            downloader=downloader,
            userid=userid,
            username=username,
            label=label,
            custom_words=custom_words,
            governance=governance,
        )
        if governance:
            governance.report(context, "submitted" if download_hash else "failed",
                              None if download_hash else "下载器未接受该资源，未提交成功")
        return (download_hash, error_msg) if return_detail else download_hash

    def _has_completed_music_download(self, prepared: _PreparedDownload, source: Optional[str]) -> bool:
        """
        按真实 hash 核验整包音频是否已成功整理交付；失败或部分整理可重试。

        交付事实来自整理历史本身：单曲被移出媒体库不再触发整包重投，仅当全部整理
        目标都已消失（专辑删除或整体迁移）时才视为交付失效。
        """
        if prepared.media.type != MediaType.MUSIC or not self._is_subscribe_source(source):
            return False
        download_hash = cast(Any, TorrentHelper)().get_torrent_hash(prepared.torrent_content)
        if not download_hash:
            return False
        history = self.download_history_repository.get_by_hash(download_hash)
        if not history or resolve_media_identity(media=history) != resolve_media_identity(media=prepared.media):
            return False
        files = self.download_history_repository.get_files_by_hash(download_hash)
        extensions = get_chain_runtime_config_snapshot().audio_extensions
        audio_files = [FileURI.from_uri(item.fullpath) for item in files
                       if item.fullpath and Path(item.fullpath).suffix.lower() in extensions]
        audio_paths = {(item.storage, item.path) for item in audio_files}
        if not audio_paths:
            return False
        delivered = self._music_delivered_audio_keys(download_hash, history.downloader)
        if not delivered:
            return False
        retained = sum(1 for alive in delivered.values() if alive)
        if not retained:
            logger.info(f"{prepared.torrent.title} 媒体库已无该批整理目标，视为交付失效并允许重新提交")
            return False
        if not audio_paths.issubset(delivered):
            return False
        if retained < len(delivered):
            # 下载器对同一 hash 只会复用任务，重投整包补不回被删单曲，只会重复记录历史。
            logger.info(
                f"{prepared.torrent.title} 媒体库缺失 {len(delivered) - retained} 首已整理曲目，沿用历史交付不重投整包"
            )
        return True

    def _music_delivered_audio_keys(
            self,
            download_hash: str,
            expected_downloader: Optional[str],
    ) -> Dict[Tuple[str, str], bool]:
        """
        按整理历史汇总该种子音频的交付事实，并标记每条目标当前是否仍在媒体库。

        :param download_hash: 种子真实 hash
        :param expected_downloader: 下载历史记录的下载器，用于排除跨下载器的历史记录
        :return: 下载源路径标识到“整理目标是否仍存在”的映射
        """
        delivered: Dict[Tuple[str, str], bool] = {}
        for item in self.transfer_history_repository.list_by_hash(download_hash):
            if not (item.status and item.dest
                    and item.type == MediaType.MUSIC.value
                    and item.downloader == expected_downloader
                    and item.dest_storage == "local"):
                continue
            # 同一曲目可能有多条整理记录，任一目标仍在媒体库即视为已交付。
            key = (item.src_storage or "local", str(item.src or ""))
            delivered[key] = delivered.get(key, False) or Path(item.dest).is_file()
        return delivered

    def _prepare_download_single(
        self,
        *,
        context: Context,
        torrent_file: Optional[Path],
        torrent_content: Optional[Union[str, bytes]],
        episodes: Optional[Set[int]],
        channel: Optional[NotificationChannel],
        source: Optional[str],
        downloader: Optional[str],
        save_path: Optional[str],
        userid: Union[str, int, None],
        username: Optional[str],
    ) -> tuple[Optional[_PreparedDownload], Optional[str]]:
        """
        补全媒体、读取种子并解析出可信下载目录。
        """
        torrent, media, meta = context.torrent_info, context.media_info, context.meta_info
        if torrent is None or media is None or meta is None:
            return None, "下载上下文缺少媒体、元数据或种子信息"
        site_downloader = torrent.site_downloader
        supplemented_media = MediaChain().supplement_tmdb_info(media, meta)
        if not isinstance(supplemented_media, (MediaInfo, MusicInfo)):
            return None, "媒体信息补全失败"
        media = supplemented_media
        context.media_info = media
        save_path, event_error = self._apply_resource_download_event(
            context, episodes, channel, source, downloader, save_path, userid, username
        )
        if event_error:
            return None, event_error
        download_episodes = episode_rules.format_ranges(list(episodes)) if episodes else None
        if episodes is not None:
            context.selected_episodes = sorted(set(episodes))
        elif meta.episode_list:
            context.selected_episodes = sorted(set(meta.episode_list))
        else:
            context.selected_episodes = []
        download_error = "下载种子内容为空"

        def remember_failure(error_msg: str) -> None:
            """
            保留真实失败原因，使无效种子和限流分别使用正确的资源冷却。
            """
            nonlocal download_error
            download_error = error_msg

        if not torrent_file and not torrent_content:
            torrent_content, _, _ = self.download_torrent(
                torrent, channel=channel, source=source, userid=userid,
                on_failure=remember_failure,
            )
        elif torrent_file:
            torrent_content = (
                torrent_file.read_bytes()
                if torrent_file.exists()
                # 与 TorrentHelper.download_torrent 的写入路由一致，种子只缓存在本地文件系统
                else FileCache(local_only=True).get(
                    torrent_file.as_posix(), region="torrents"
                )
            )
        if not torrent_content:
            self._record_download_failure(
                context=context,
                error_msg=download_error,
                downloader=downloader or site_downloader,
                source=source,
                episodes=episodes,
            )
            return None, download_error
        folder_name, file_list = cast(Any, TorrentHelper)().get_fileinfo_from_torrent_content(
            torrent_content
        )
        self._record_music_album_track_keys(context, file_list)
        storage, download_dir, error_msg = self._resolve_media_download_dir(
            media_info=media,
            save_path=save_path,
        )
        if not download_dir or not storage:
            if error_msg == "未找到下载目录":
                self.messagehelper.put(
                    f"{(media.type.value if media.type else "")} {media.title_year} 未找到下载目录！",
                    title="下载失败",
                    role="system",
                    level="error",
                )
            return None, error_msg or "未找到下载目录"
        download_uri = FileURI(storage=storage, path=download_dir.as_posix()).uri
        return _PreparedDownload(
            torrent=torrent,
            media=media,
            meta=meta,
            torrent_content=torrent_content,
            folder_name=folder_name,
            file_list=file_list,
            download_dir=Path(download_uri),
            download_uri=download_uri,
            download_episodes=download_episodes,
            site_downloader=site_downloader,
        ), None

    def _submit_prepared_download(
        self,
        *,
        prepared: _PreparedDownload,
        context: Context,
        episodes: Optional[Set[int]],
        channel: Optional[NotificationChannel],
        source: Optional[str],
        downloader: Optional[str],
        userid: Union[str, int, None],
        username: Optional[str],
        label: Optional[str],
        custom_words: Optional[str],
        governance: Optional[SubscriptionDownloadGovernance],
    ) -> tuple[Optional[str], Optional[str]]:
        """
        在可取消边界后调用下载器，并分派成功或拒绝结算。
        """
        if self._subscription_download_cancelled(governance):
            return None, "订阅下载在提交前已取消"
        # 非订阅入口清除复用上下文上的旧标记；订阅来源在插件处理与选集之后冻结。
        context.subscription_download = None
        if governance and governance.prepare_submission:
            governance.prepare_submission(context)
        submission_label = subscription_download_label(context.subscription_download)
        if submission_label:
            logger.info(f"开始下载【{submission_label}】{prepared.torrent.title} ...")
        if governance and governance.mark_started:
            governance.mark_started()
        category = prepared.media.category
        # 音乐尚无目录分类时仍提供稳定的下载器分类，不回写媒体的目录或元数据分类。
        if not category and prepared.media.type == MediaType.MUSIC:
            category = MediaType.MUSIC.value
        result = self.download(
            content=prepared.torrent_content,
            cookie=prepared.torrent.site_cookie,
            episodes=cast(Set[int], episodes),
            download_dir=prepared.download_dir,
            category=category,
            label=label,
            downloader=downloader or prepared.site_downloader,
            custom_words=custom_words,
        )
        actual_downloader, download_hash, layout, error_msg = (
            result if result else (None, None, None, "未找到下载器")
        )
        if download_hash:
            self._settle_accepted_download(
                prepared=prepared,
                context=context,
                episodes=episodes,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                custom_words=custom_words,
                actual_downloader=actual_downloader,
                download_hash=download_hash,
                layout=layout,
                already_exists=error_msg == DOWNLOAD_TASK_ALREADY_EXISTS,
            )
        else:
            self._record_rejected_download(
                prepared=prepared,
                context=context,
                episodes=episodes,
                channel=channel,
                source=source,
                downloader=downloader,
                userid=userid,
                actual_downloader=actual_downloader,
                error_msg=error_msg,
            )
        return download_hash, error_msg

    def _settle_accepted_download(
        self,
        *,
        prepared: _PreparedDownload,
        context: Context,
        episodes: Optional[Set[int]],
        channel: Optional[NotificationChannel],
        source: Optional[str],
        userid: Union[str, int, None],
        username: Optional[str],
        custom_words: Optional[str],
        actual_downloader: Optional[str],
        download_hash: str,
        layout: Optional[str],
        already_exists: bool = False,
    ) -> None:
        """
        按普通下载合同结算下载器明确返回的成功结果。

        :param already_exists: 下载器中已存在同一任务时为 True，结算时复用现有任务而不重复通知
        """
        if already_exists:
            logger.info(f"{prepared.torrent.title} 下载器中已存在同一任务，沿用现有下载进度")
        # 下载器中已存在相同 Hash 时下载器会沿用原保存目录，须以实际目录记录
        download_dir = self._resolve_actual_download_dir(
            downloader=actual_downloader,
            download_hash=download_hash,
            fallback=prepared.download_dir,
        )
        self._settle_download_success(
            context=context,
            media=prepared.media,
            meta=prepared.meta,
            torrent=prepared.torrent,
            folder_name=prepared.folder_name,
            file_list=prepared.file_list,
            download_dir=download_dir,
            layout=layout,
            downloader=actual_downloader,
            download_hash=download_hash,
            download_episodes=prepared.download_episodes,
            episodes=episodes,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            torrent_content=prepared.torrent_content,
            custom_words=custom_words,
            already_exists=already_exists,
        )

    def _resolve_actual_download_dir(
        self,
        *,
        downloader: Optional[str],
        download_hash: str,
        fallback: Path,
    ) -> Path:
        """
        获取下载器中该任务的真实保存目录，作为下载历史与文件明细的落库路径。

        下载器中已存在相同 Hash 的任务时（例如刷流插件先下载），下载器会沿用
        其原有保存目录；此时按预期下载目录记录会得到磁盘上不存在的路径，后续
        整理与手动整理都无法关联该文件。仅本地绝对路径需要回读，远程存储的
        目录由对应存储协议自身管理，保持原值不变。
        """
        if not download_hash or not fallback.is_absolute():
            return fallback
        try:
            torrents = self.list_torrents(hashs=[download_hash], downloader=downloader)
        except Exception as err:
            logger.debug(f"查询下载任务实际保存目录失败：{str(err)}")
            return fallback
        if not torrents:
            return fallback
        torrent = next(
            (
                item for item in torrents
                if str(getattr(item, "hash", "")) == str(download_hash)
            ),
            torrents[0],
        )
        save_path = getattr(torrent, "save_path", None)
        if not save_path:
            return fallback
        actual_dir = Path(str(save_path))
        if not actual_dir.is_absolute():
            return fallback
        if actual_dir != fallback:
            logger.warn(
                f"下载器中已存在该任务，实际保存目录 {actual_dir.as_posix()} 与预期 "
                f"{fallback.as_posix()} 不一致，按实际目录记录下载历史"
            )
        return actual_dir

    def _record_rejected_download(
        self,
        *,
        prepared: _PreparedDownload,
        context: Context,
        episodes: Optional[Set[int]],
        channel: Optional[NotificationChannel],
        source: Optional[str],
        downloader: Optional[str],
        userid: Union[str, int, None],
        actual_downloader: Optional[str],
        error_msg: str,
    ) -> None:
        """
        按普通失败合同记录下载器明确拒绝并通知原调用渠道。
        """
        logger.error(
            f"{prepared.media.title_year} 添加下载任务失败："
            f"{prepared.torrent.title} - {prepared.torrent.enclosure}，{error_msg}"
        )
        self._record_download_failure(
            context=context,
            error_msg=error_msg,
            downloader=actual_downloader or downloader or prepared.site_downloader,
            source=source,
            episodes=episodes,
        )
        self.post_message(
            Message(
                channel=channel,
                source=source if channel else None,
                mtype=MessageType.Manual,
                title=f"添加下载任务失败：{prepared.media.title_year} {prepared.meta.season_episode}",
                text=(
                    f"站点：{prepared.torrent.site_name}\n"
                    f"种子名称：{prepared.meta.org_string}\n"
                    f"错误信息：{error_msg}"
                ),
                image=prepared.media.get_message_image(),
                poster=prepared.media.get_poster_image() or None,
                userid=userid,
            )
        )
