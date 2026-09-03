"""种子获取与单任务提交 owner。"""

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple, Union, cast
from urllib.parse import urlencode, urljoin, urlparse

from app.application.configuration import get_chain_runtime_config_snapshot
from app.application.directory import validate_download_save_path
from app.application.download.admission import SubscriptionDownloadGovernance
from app.application.torrent.download import TorrentHelper
from app.chain.download.contract import _DownloadOwnerBase
from app.chain.download.ports import (
    _close_download_response,
    _download_ports_snapshot,
)
from app.chain.media import MediaChain
from app.domain import episode as episode_rules
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
from app.schemas.event import ResourceDownloadEventData
from app.schemas.file import FileURI
from app.schemas.message import Message
from app.schemas.types import (
    ChainEventType,
    MessageType,
    NotificationChannel,
)


@dataclass(frozen=True, slots=True)
class _PreparedDownload:
    """下载器调用前已经验证并规范化的本地提交事实。"""

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
    """种子获取、间接地址解析与资源下载事件 owner。"""


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
        """解析索引器编码的换票请求，并返回可信来源的临时下载地址。"""
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
                         userid: Union[str, int, None] = None
                         ) -> Tuple[Optional[Union[str, bytes]], str, list[str]]:
        """
        下载种子文件，如果是磁力链，会返回磁力链接本身
        :return: 种子内容，种子目录名，种子文件清单
        """
        # 获取下载链接
        if not torrent.enclosure:
            return None, "", []
        if torrent.enclosure.startswith("magnet:"):
            return torrent.enclosure, "", []
        # Cookie
        site_cookie: Optional[str] = torrent.site_cookie
        indirect_download = torrent.enclosure.startswith("[")
        if indirect_download:
            # 需要解码获取下载地址
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
            return None, "", []
        # 下载种子文件
        _, content, download_folder, files, error_msg = cast(
            Any, TorrentHelper
        )().download_torrent(
            url=torrent_url,
            cookie=site_cookie,
            ua=torrent.site_ua or self.runtime_config.user_agent,
            proxy=torrent.site_proxy,
            cache_invalid=not indirect_download)

        if isinstance(content, str):
            # 磁力链
            return content, "", []

        if not content:
            logger.error(f"下载种子文件失败：{torrent.title}")
            self.post_message(Message(
                channel=channel,
                source=source if channel else None,
                mtype=MessageType.Manual,
                title=f"{torrent.title} 种子下载失败！",
                text=f"错误信息：{error_msg}\n站点：{torrent.site_name}",
                userid=userid))
            return None, "", []

        # 返回 种子文件路径，种子目录名，种子文件清单
        return content, download_folder or "", files or []

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
        """应用资源下载事件覆盖，并校验事件返回的下载目录。"""
        meta = context.meta_info
        media = context.media_info
        event_data = ResourceDownloadEventData(
            context=context,
            episodes=episodes or (meta.episode_list if meta else []),
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
                save_path = cast(Optional[str], event_data.options.get("save_path"))
        if save_path is None:
            return None, None
        try:
            return validate_download_save_path(save_path), None
        except ValueError as err:
            logger.warn(str(err))
            return save_path, str(err)


class DownloadSubmissionOwner(_DownloadResourceOwner):
    """单任务下载准备、提交与结算 owner。"""

    @staticmethod
    def _subscription_download_cancelled(
        governance: Optional[SubscriptionDownloadGovernance],
    ) -> bool:
        """在下载器副作用前读取订阅执行上下文的停止信号。"""
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
        """准备下载事实，提交下载器并按订阅治理合同结算结果。"""
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
            return (None, error_msg) if return_detail else None
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
        return (download_hash, error_msg) if return_detail else download_hash

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
        """补全媒体、读取种子并解析出可信下载目录。"""
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
        if not torrent_file and not torrent_content:
            torrent_content, _, _ = self.download_torrent(
                torrent, channel=channel, source=source, userid=userid
            )
        elif torrent_file:
            torrent_content = (
                torrent_file.read_bytes()
                if torrent_file.exists()
                else FileCache().get(torrent_file.as_posix(), region="torrents")
            )
        if not torrent_content:
            self._record_download_failure(
                context=context,
                error_msg="下载种子内容为空",
                downloader=downloader or site_downloader,
                source=source,
                episodes=episodes,
            )
            return None, "下载种子内容为空"
        folder_name, file_list = cast(Any, TorrentHelper)().get_fileinfo_from_torrent_content(
            torrent_content
        )
        album_error = self._validate_music_album_resource(context, file_list)
        if album_error:
            logger.info(f"{torrent.title} {album_error}，跳过该资源")
            self._record_download_failure(
                context=context,
                error_msg=album_error,
                downloader=downloader or site_downloader,
                source=source,
                episodes=episodes,
            )
            return None, album_error
        storage, download_dir, error_msg = self._resolve_media_download_dir(
            media_info=media,
            save_path=save_path,
        )
        if not download_dir or not storage:
            if error_msg == "未找到下载目录":
                self.messagehelper.put(
                    f"{media.type.value} {media.title_year} 未找到下载目录！",
                    title="下载失败",
                    role="system",
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
        """在可取消边界后调用下载器，并分派成功或拒绝结算。"""
        if self._subscription_download_cancelled(governance):
            return None, "订阅下载在提交前已取消"
        if governance and governance.mark_started:
            governance.mark_started()
        result = self.download(
            content=prepared.torrent_content,
            cookie=prepared.torrent.site_cookie,
            episodes=cast(Set[int], episodes),
            download_dir=prepared.download_dir,
            category=prepared.media.category,
            label=label,
            downloader=downloader or prepared.site_downloader,
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
    ) -> None:
        """按普通下载合同结算下载器明确返回的成功结果。"""
        self._settle_download_success(
            context=context,
            media=prepared.media,
            meta=prepared.meta,
            torrent=prepared.torrent,
            folder_name=prepared.folder_name,
            file_list=prepared.file_list,
            download_dir=prepared.download_dir,
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
        )

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
        """按普通失败合同记录下载器明确拒绝并通知原调用渠道。"""
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
                userid=userid,
            )
        )
