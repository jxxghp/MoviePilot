"""种子获取与单任务提交 owner。"""

import base64
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple, Union, cast
from urllib.parse import urlencode, urljoin, urlparse

from app.application.configuration import get_chain_runtime_config_snapshot
from app.application.directory import validate_download_save_path
from app.application.download.admission import (
    DownloadReconciliationRequired,
    SubscriptionDownloadGovernance,
)
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


class DownloadSubmissionOwner(_DownloadOwnerBase):
    """种子获取与单任务提交 owner。"""


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

    def _execute_download_single(self, context: Context,
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
        下载及发送通知
        :param context: 资源上下文
        :param torrent_file: 种子文件路径
        :param torrent_content: 种子内容（磁力链或种子文件内容）
        :param episodes: 需要下载的集数
        :param channel: 通知渠道
        :param source: 来源（消息通知、Subscribe、Manual等）
        :param downloader: 下载器
        :param save_path: 保存路径, 支持<storage>:<path>, 如rclone:/MP, smb:/server/share/Movies等
        :param userid: 用户ID
        :param username: 调用下载的用户名/插件名
        :param label: 自定义标签
        :param return_detail: 是否返回详细结果；False 时返回下载任务 hash 或 None，True 时返回 (hash, error_msg)
        :param custom_words: 下载来源（如订阅）的完整自定义识别词文本，随下载记录存档，供整理时原样复现识别
        :param governance: 订阅级提交幂等、入口任务与取消检查；非订阅调用保持为空
        :return: return_detail=False 时返回下载任务 hash 或 None；return_detail=True 时返回 (hash, error_msg)
        """
        _torrent = context.torrent_info
        _media = context.media_info
        _meta = context.meta_info
        if _torrent is None or _media is None or _meta is None:
            error_message = "下载上下文缺少媒体、元数据或种子信息"
            return (None, error_message) if return_detail else None
        _site_downloader = _torrent.site_downloader

        # 下载目录和下载器分类依赖 TMDB 辅助分类，但媒体主身份保持不变。
        supplemented_media = MediaChain().supplement_tmdb_info(_media, _meta)
        if not isinstance(supplemented_media, (MediaInfo, MusicInfo)):
            error_message = "媒体信息补全失败"
            return (None, error_message) if return_detail else None
        _media = supplemented_media
        context.media_info = _media

        save_path, event_error = self._apply_resource_download_event(
            context, episodes, channel, source, downloader, save_path,
            userid, username,
        )
        if event_error:
            return (None, event_error) if return_detail else None

        # 实际下载的集数
        download_episodes = episode_rules.format_ranges(list(episodes)) if episodes else None
        if episodes is not None:
            context.selected_episodes = sorted(set(episodes))
        elif _meta and _meta.episode_list:
            context.selected_episodes = sorted(set(_meta.episode_list))
        else:
            context.selected_episodes = []
        _folder_name = ""
        if not torrent_file and not torrent_content:
            # 下载种子文件，得到的可能是文件也可能是磁力链
            torrent_content, _folder_name, _file_list = self.download_torrent(_torrent,
                                                                              channel=channel,
                                                                              source=source,
                                                                              userid=userid)
        elif torrent_file:
            if torrent_file.exists():
                torrent_content = torrent_file.read_bytes()
            else:
                # 缓存处理器
                cache_backend = FileCache()
                # 读取缓存的种子文件
                torrent_content = cache_backend.get(torrent_file.as_posix(), region="torrents")

        if not torrent_content:
            self._record_download_failure(
                context=context,
                error_msg="下载种子内容为空",
                downloader=downloader or _site_downloader,
                source=source,
                episodes=episodes,
            )
            return (None, "下载种子内容为空") if return_detail else None

        # 获取种子文件的文件夹名和文件清单
        _folder_name, _file_list = cast(
            Any, TorrentHelper
        )().get_fileinfo_from_torrent_content(torrent_content)

        album_validation_error = self._validate_music_album_resource(context, _file_list)
        if album_validation_error:
            logger.info(f"{_torrent.title} {album_validation_error}，跳过该资源")
            self._record_download_failure(
                context=context,
                error_msg=album_validation_error,
                downloader=downloader or _site_downloader,
                source=source,
                episodes=episodes,
            )
            return (None, album_validation_error) if return_detail else None

        storage, download_dir, error_msg = self._resolve_media_download_dir(
            media_info=_media,
            save_path=save_path,
        )
        if not download_dir or not storage:
            if error_msg == "未找到下载目录":
                self.messagehelper.put(f"{_media.type.value} {_media.title_year} 未找到下载目录！",
                                       title="下载失败", role="system")
            return (None, error_msg or "未找到下载目录") if return_detail else None
        file_uri = FileURI(storage=storage, path=download_dir.as_posix())
        download_dir = Path(file_uri.uri)

        if self._subscription_download_cancelled(governance):
            cancel_error = "订阅下载在提交前已取消"
            return (None, cancel_error) if return_detail else None
        admission, duplicate_hash = self._claim_subscription_download(
            context=context,
            episodes=episodes,
            governance=governance,
            downloader=downloader or _site_downloader,
            download_uri=file_uri.uri,
        )
        if duplicate_hash:
            if governance and governance.mark_started:
                governance.mark_started()
            logger.info(f"{_torrent.title} 已由重叠订阅入口提交，复用任务 {duplicate_hash}")
            return (duplicate_hash, "下载任务已由重叠入口提交") if return_detail else duplicate_hash
        if admission is not None and not admission.acquired:
            wait_error = (
                f"订阅下载提交当前为 {admission.snapshot.state}，"
                f"最早可重试：{admission.snapshot.available_at or '待下一轮'}"
            )
            return (None, wait_error) if return_detail else None
        attempt_token = admission.snapshot.attempt_token if admission is not None else None
        admission_key = admission.snapshot.idempotency_key if admission is not None else None
        if admission is not None and self._subscription_download_cancelled(governance):
            self._subscription_download_repository().mark_cancelled(
                idempotency_key=admission.snapshot.idempotency_key,
                attempt_token=admission.snapshot.attempt_token or "",
            )
            cancel_error = "订阅下载在下载器调用前已取消"
            return (None, cancel_error) if return_detail else None

        # 添加下载
        if governance and governance.mark_started:
            governance.mark_started()
        try:
            result: Optional[Tuple[Optional[str], Optional[str], Optional[str], str]] = self.download(
                content=torrent_content,
                cookie=_torrent.site_cookie,
                episodes=cast(Set[int], episodes),
                download_dir=download_dir,
                category=_media.category,
                label=label,
                downloader=downloader or _site_downloader,
            )
        except Exception as err:
            if admission_key and attempt_token:
                self._subscription_download_repository().mark_reconcile_required(
                    idempotency_key=admission_key,
                    attempt_token=attempt_token,
                    error=f"下载器调用异常：{str(err)}",
                )
                raise DownloadReconciliationRequired(
                    f"{_torrent.title} 下载器结果不确定，已冻结自动重试"
                ) from err
            raise
        if result:
            _downloader, _hash, _layout, error_msg = result
        else:
            _downloader, _hash, _layout, error_msg = None, None, None, "未找到下载器"

        if _hash:
            if admission_key and attempt_token:
                accepted = self._subscription_download_repository().mark_accepted(
                    idempotency_key=admission_key,
                    attempt_token=attempt_token,
                    downloader=_downloader,
                    download_hash=_hash,
                )
                if not accepted:
                    raise DownloadReconciliationRequired(
                        f"{_torrent.title} 已被下载器接受，但本地接受状态写入失败"
                    )
            try:
                self._settle_download_success(
                    context=context,
                    media=_media,
                    meta=_meta,
                    torrent=_torrent,
                    folder_name=_folder_name,
                    file_list=_file_list,
                    download_dir=download_dir,
                    layout=_layout,
                    downloader=_downloader,
                    download_hash=_hash,
                    download_episodes=download_episodes,
                    episodes=episodes,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    torrent_content=torrent_content,
                    custom_words=custom_words,
                )
            except Exception as err:
                if admission_key and attempt_token:
                    self._subscription_download_repository().mark_reconcile_required(
                        idempotency_key=admission_key,
                        attempt_token=attempt_token,
                        error=f"下载器已接受但本地结算失败：{str(err)}",
                        downloader=_downloader,
                        download_hash=_hash,
                    )
                    raise DownloadReconciliationRequired(
                        f"{_torrent.title} 已被下载器接受但本地结算失败，已转待对账"
                    ) from err
                raise
            if admission_key and attempt_token:
                succeeded = self._subscription_download_repository().mark_succeeded(
                    idempotency_key=admission_key,
                    attempt_token=attempt_token,
                )
                if not succeeded:
                    self._subscription_download_repository().mark_reconcile_required(
                        idempotency_key=admission_key,
                        attempt_token=attempt_token,
                        error="本地结算完成但幂等成功终态写入失败",
                        downloader=_downloader,
                        download_hash=_hash,
                    )
                    raise DownloadReconciliationRequired(
                        f"{_torrent.title} 本地结算完成但提交终态未确认，已转待对账"
                    )
        else:
            if admission_key and attempt_token:
                retry_at = self._subscription_download_retry_at(
                    error_msg,
                    self._download_failure_ttl(error_msg),
                )
                self._subscription_download_repository().mark_retryable(
                    idempotency_key=admission_key,
                    attempt_token=attempt_token,
                    available_at=retry_at,
                    error=error_msg,
                )
            # 下载失败
            logger.error(f"{_media.title_year} 添加下载任务失败："
                         f"{_torrent.title} - {_torrent.enclosure}，{error_msg}")
            self._record_download_failure(
                context=context,
                error_msg=error_msg,
                downloader=_downloader or downloader or _site_downloader,
                source=source,
                episodes=episodes,
            )
            # 只发送给对应渠道和用户
            self.post_message(Message(
                channel=channel,
                source=source if channel else None,
                mtype=MessageType.Manual,
                title="添加下载任务失败：%s %s"
                      % (_media.title_year, _meta.season_episode),
                text=f"站点：{_torrent.site_name}\n"
                     f"种子名称：{_meta.org_string}\n"
                     f"错误信息：{error_msg}",
                image=_media.get_message_image(),
                userid=userid))
        if return_detail:
            return _hash, error_msg
        return _hash
