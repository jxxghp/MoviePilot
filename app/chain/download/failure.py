"""下载失败指纹、冷却和持久化 owner。"""

import hashlib
import json
import re
import time
from typing import Dict, List, Optional, Set
from urllib.parse import parse_qs, urlparse

from app.application.download.failures import (
    DownloadFailureRepository,
    DownloadFailureSnapshot,
    DownloadFailureWrite,
)
from app.chain.download.contract import _DownloadOwnerBase
from app.domain import episode as episode_rules
from app.domain.context import (
    Context,
    TorrentInfo,
)
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.schemas.media import build_media_key, resolve_media_identity

DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS = 24 * 60 * 60
DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS = 60 * 60
DOWNLOAD_FAILURE_RESOURCE_ERROR_KEYWORDS = (
    "无法读取种子文件",
    "下载种子内容为空",
    "无法获取下载地址",
    "种子下载失败",
    "torrent not found",
    "not found",
    "404",
    "deleted",
    "invalid torrent",
    "专辑资源",
)

class DownloadFailureOwner(_DownloadOwnerBase):
    """下载失败指纹、冷却和持久化 owner。"""


    @staticmethod
    def _is_subscribe_source(source: Optional[str]) -> bool:
        """
        判断下载来源是否为订阅任务。
        """
        return bool(source and str(source).startswith("Subscribe|"))

    @staticmethod
    def _format_failure_episodes(meta: Optional[MetaBase]) -> Optional[str]:
        """
        从识别元数据中格式化用于失败记录的集数。
        """
        if not meta:
            return None
        if getattr(meta, "episode", None):
            return meta.episode
        episode_list = getattr(meta, "episode_list", None)
        if episode_list:
            return episode_rules.format_ranges(list(episode_list))
        return None

    @staticmethod
    def _torrent_resource_key(torrent: Optional[TorrentInfo]) -> str:
        """
        生成不保存敏感下载链接的种子资源键。
        """
        if not torrent:
            return ""
        for attr_name in ("torrent_id", "info_hash"):
            value = getattr(torrent, attr_name, None)
            if value:
                return str(value)

        for attr_name in ("page_url", "enclosure"):
            url = getattr(torrent, attr_name, None)
            if not url:
                continue
            match = re.search(r"\[(.*?)](.*)", str(url))
            if match:
                url = match.group(2)
            parsed = urlparse(str(url))
            params = parse_qs(parsed.query)
            for param_name in ("id", "torrentid", "torrent_id", "tid", "hash"):
                values = params.get(param_name)
                if values:
                    return f"{parsed.netloc}:{param_name}={values[0]}"
            if parsed.netloc and parsed.path:
                return f"{parsed.netloc}{parsed.path}"

        title = getattr(torrent, "title", "") or ""
        size = getattr(torrent, "size", "") or ""
        return f"title={title}|size={size}"

    @classmethod
    def _build_download_failure_fingerprint(cls, context: Context) -> Optional[str]:
        """
        根据媒体和种子资源信息生成失败冷却指纹。
        """
        media = getattr(context, "media_info", None)
        torrent = getattr(context, "torrent_info", None)
        if not media or not torrent:
            return None

        media_type = getattr(getattr(media, "type", None), "value", getattr(media, "type", None))
        media_source, media_id = resolve_media_identity(media=media)
        media_key = build_media_key(media_source, media_id) or (
            f"{getattr(media, 'title', '')}:{getattr(media, 'year', '')}"
        )
        meta = getattr(context, "meta_info", None)
        site = getattr(torrent, "site", None) or getattr(torrent, "site_name", None)
        meta_season = getattr(meta, "season", None)
        media_season = getattr(media, "season", None)
        season = meta_season if meta_season is not None else media_season
        payload = {
            "media_type": str(media_type or ""),
            "media_key": str(media_key or ""),
            "season": str(season) if season is not None else "",
            "episodes": cls._format_failure_episodes(meta) or "",
            "site": str(site or ""),
            "resource": cls._torrent_resource_key(torrent),
        }
        if not payload["media_type"] or not payload["media_key"] or not payload["resource"]:
            return None
        raw_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    @staticmethod
    def _download_failure_ttl(error_msg: Optional[str]) -> int:
        """
        按失败原因确定资源冷却时间。
        """
        error_text = str(error_msg or "").lower()
        if any(keyword in error_text for keyword in DOWNLOAD_FAILURE_RESOURCE_ERROR_KEYWORDS):
            return DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS
        return DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS

    def _record_download_failure(
            self,
            context: Context,
            error_msg: Optional[str],
            downloader: Optional[str] = None,
            source: Optional[str] = None,
            episodes: Optional[Set[int]] = None,
    ) -> Optional[str]:
        """
        记录资源级下载失败，并返回本次失败指纹。
        """
        fingerprint = self._build_download_failure_fingerprint(context)
        if not fingerprint:
            return None

        now_timestamp = time.time()
        now_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_timestamp))
        next_retry_at = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(now_timestamp + self._download_failure_ttl(error_msg)),
        )
        media = context.media_info
        media_source, media_id = resolve_media_identity(media=media)
        meta = context.meta_info
        torrent = context.torrent_info
        site = getattr(torrent, "site", None)
        try:
            repository: DownloadFailureRepository = self.download_failure_repository
            repository.record_failure(
                DownloadFailureWrite(
                    fingerprint=fingerprint,
                    failed_at=now_time,
                    next_retry_at=next_retry_at,
                    media_type=getattr(
                        getattr(media, "type", None),
                        "value",
                        getattr(media, "type", None),
                    ),
                    title=getattr(media, "title", None),
                    year=getattr(media, "year", None),
                    media_source=media_source,
                    media_id=media_id,
                    seasons=(
                        str(getattr(meta, "season", None))
                        if getattr(meta, "season", None) is not None
                        else None
                    ),
                    episodes=(
                        episode_rules.format_ranges(list(episodes))
                        if episodes
                        else self._format_failure_episodes(meta)
                    ),
                    site=site if isinstance(site, int) else None,
                    site_name=getattr(torrent, "site_name", None),
                    torrent_id=self._torrent_resource_key(torrent),
                    torrent_name=getattr(torrent, "title", None),
                    torrent_size=getattr(torrent, "size", None),
                    downloader=downloader,
                    source=str(source)[:1000] if source else None,
                    error_message=str(error_msg or "")[:1000],
                )
            )
        except Exception as err:
            logger.error(f"记录下载失败冷却失败：{str(err)}")
        return fingerprint

    def _active_download_failure_fingerprints(
            self,
            contexts: List[Context],
            source: Optional[str],
    ) -> Dict[str, DownloadFailureSnapshot]:
        """
        查询当前订阅候选中仍处于冷却期的失败记录，返回指纹到失败记录的映射。
        """
        if not self._is_subscribe_source(source):
            return {}
        fingerprints = [
            fingerprint
            for fingerprint in [
                self._build_download_failure_fingerprint(context)
                for context in contexts or []
            ]
            if fingerprint
        ]
        if not fingerprints:
            return {}
        now_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        try:
            repository: DownloadFailureRepository = self.download_failure_repository
            return repository.get_active_by_fingerprints(
                fingerprints=fingerprints, now_time=now_time,
            )
        except Exception as err:
            logger.error(f"查询下载失败冷却失败：{str(err)}")
            return {}

    @staticmethod
    def _log_download_failure_cooldown(
            context: Context,
            failure: Optional[DownloadFailureSnapshot],
    ) -> None:
        """记录候选资源处于失败冷却期时的跳过原因和下次重试时间。"""
        reason = getattr(failure, "error_message", None) or "未知原因"
        retry_at = getattr(failure, "next_retry_at", None)
        if retry_at:
            logger.info(
                f"{context.torrent_info.title} 近期添加下载失败（失败原因：{reason}），"
                f"暂时跳过该资源，将于 {retry_at} 后重试"
            )
        else:
            logger.info(
                f"{context.torrent_info.title} 近期添加下载失败（失败原因：{reason}），"
                "暂时跳过该资源"
            )
