"""下载候选规范化、排序和媒体选择 owner。"""

from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple, Union, cast

from app.application.configuration import get_chain_runtime_config_snapshot
from app.application.download.admission import SubscriptionDownloadGovernance
from app.application.download.failures import (
    DownloadFailureSnapshot,
)
from app.application.torrent.download import TorrentHelper
from app.chain.download.contract import _DownloadOwnerBase
from app.domain.context import (
    Context,
    MediaInfo,
)
from app.domain.meta.metamusic import MetaMusic
from app.foundation import text as text_tools
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.event import ResourceSelectionEventData
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    ChainEventType,
    MediaType,
    NotificationChannel,
)


def _new_torrent_helper() -> TorrentHelper:
    """构造保留动态初始化行为但具有静态返回类型的种子助手。"""
    factory = cast(Callable[[], TorrentHelper], TorrentHelper)
    return factory()


class DownloadSelectionOwner(_DownloadOwnerBase):
    """下载候选规范化、排序和媒体选择 owner。"""


    @classmethod
    def _validate_music_album_resource(
            cls,
            context: Context,
            file_list: Optional[List[str]],
    ) -> Optional[str]:
        """校验专辑种子是否包含预期数量的独立音轨，并标记已确认的整专覆盖。"""
        media = context.media_info
        if (
                not media
                or media.type != MediaType.MUSIC
                or getattr(media, "music_type", None) != MUSIC_ENTITY_ALBUM
        ):
            return None

        context.confirmed_full_coverage = False
        try:
            expected_tracks = int(getattr(media, "total_tracks", None) or 0)
        except (TypeError, ValueError):
            expected_tracks = 0
        if expected_tracks <= 0:
            return "专辑资源无法校验：专辑总曲目数未知"
        if not file_list:
            return "专辑资源无法校验：种子未提供文件清单，不能确认整专曲目"

        track_identities = {
            identity
            for file in file_list
            if Path(str(file)).suffix.lower()
            in get_chain_runtime_config_snapshot().audio_extensions
            and (identity := cls._music_resource_track_identity(file))
        }
        actual_tracks = len(track_identities)
        if actual_tracks < expected_tracks:
            return (
                f"专辑资源不完整：专辑共 {expected_tracks} 首，"
                f"种子仅包含 {actual_tracks} 个独立音频文件"
            )

        context.confirmed_full_coverage = True
        return None

    @staticmethod
    def _music_resource_track_identity(file: str) -> Optional[Tuple[int, Union[int, str]]]:
        """从资源文件路径提取盘号和曲序，缺少曲序时按归一化曲名去重。"""
        file_path = Path(str(file))
        file_meta = MetaMusic(
            org_string=file_path.name,
            title=file_path.stem,
        ).apply_path_context(file_path)
        track_identity: Union[int, str, None] = file_meta.track_number
        if track_identity is None:
            track_identity = text_tools.normalize_upper(file_meta.title or file_path.stem)
        if track_identity in (None, ""):
            return None
        return file_meta.disc_number or 1, track_identity

    @staticmethod
    def _media_identity_keys(media: Optional[MediaInfo]) -> Set[str]:
        """返回媒体的统一身份键，用于临时缺失集映射匹配。"""
        if not media:
            return set()
        source, media_id = resolve_media_identity(media=media)
        media_key = build_media_key(source, media_id)
        return {media_key} if media_key else set()

    @classmethod
    def _matches_media_identity(cls, media: Optional[MediaInfo], media_key: object) -> bool:
        """判断媒体是否命中来源与原生 ID 组成的统一身份键。"""
        return media_key is not None and str(media_key) in cls._media_identity_keys(media)

    def _prepare_batch_download_contexts(
        self,
        contexts: List[Context],
        downloader: Optional[str],
        source: Optional[str],
    ) -> Tuple[List[Context], Dict[str, Optional[DownloadFailureSnapshot]]]:
        """
        执行批量下载前的资源选择、排序和失败冷却准备。

        :return: 排序后的上下文和本轮失败冷却记录；资源选择事件仍可替换上下文列表
        """
        logger.debug(f"Initial contexts: {len(contexts)} items, Downloader: {downloader}")
        event_data = ResourceSelectionEventData(
            contexts=contexts,
            downloader=downloader,
            origin=source,
        )
        event = self.eventmanager.send_event(ChainEventType.ResourceSelection, event_data)
        if event and event.event_data:
            event_data = event.event_data
            if event_data.updated and event_data.updated_contexts is not None:
                logger.debug(
                    f"Contexts updated by event: {len(event_data.updated_contexts)} "
                    f"items (source: {event_data.source})"
                )
                contexts = event_data.updated_contexts
        contexts = _new_torrent_helper().sort_torrents(contexts)
        active_failures: Dict[str, Optional[DownloadFailureSnapshot]] = {
            fingerprint: failure
            for fingerprint, failure in self._active_download_failure_fingerprints(
                contexts=contexts,
                source=source,
            ).items()
        }
        return contexts, active_failures

    def _download_movie_music_candidates(
        self,
        contexts: List[Context],
        downloaded_list: List[Context],
        active_failure_records: Dict[str, Optional[DownloadFailureSnapshot]],
        save_path: Optional[str],
        channel: Optional[NotificationChannel],
        source: Optional[str],
        userid: Optional[str],
        username: Optional[str],
        downloader: Optional[str],
        custom_words: Optional[str],
        governance: Optional[SubscriptionDownloadGovernance],
    ) -> None:
        """
        处理电影与音乐的直接候选下载。

        两类媒体都遵循成功后按身份去重、失败后继续尝试后续候选的规则，差异只在去重键。
        """
        downloaded_keys: Dict[MediaType, Set[str]] = {
            MediaType.MOVIE: set(),
            MediaType.MUSIC: set(),
        }
        for context in contexts:
            if runtime_stop_state.is_system_stopped:
                break
            media = context.media_info
            if media is None:
                continue
            media_type = media.type
            if media_type not in downloaded_keys:
                continue
            fingerprint = self._build_download_failure_fingerprint(context)
            if fingerprint and fingerprint in active_failure_records:
                self._log_download_failure_cooldown(
                    context,
                    active_failure_records[fingerprint],
                )
                continue
            if media_type == MediaType.MOVIE:
                download_key = media.title_year
                label = "电影"
            else:
                media_source, media_id = resolve_media_identity(media=media)
                download_key = build_media_key(media_source, media_id) or media.title_year
                label = "音乐"
            if download_key in downloaded_keys[media_type]:
                continue
            logger.info(f"开始下载{label} {context.torrent_info.title} ...")
            if self.download_single(
                context,
                save_path=save_path,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                downloader=downloader,
                custom_words=custom_words,
                governance=governance,
            ):
                logger.info(f"{context.torrent_info.title} 添加下载成功")
                downloaded_list.append(context)
                downloaded_keys[media_type].add(download_key)
            elif fingerprint:
                active_failure_records[fingerprint] = None
