"""
下载资源级失败与退避指纹、冷却和持久化 owner。
"""

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
from app.domain import torrent as torrent_rules
from app.domain.context import (
    Context,
    TorrentInfo,
)
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.schemas.media import build_media_key, resolve_media_identity

DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS = 24 * 60 * 60
DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS = 60 * 60
# 重复提交退避只压制单个资源指纹，必须长于订阅重新选源的周期，否则同一个整包仍会被反复接受。
DOWNLOAD_FAILURE_DUPLICATE_TTL_SECONDS = 7 * 24 * 60 * 60
# 退避记录写入台账的成因说明，候选跳过日志直接引用它，不能再假设这是一次失败。
DOWNLOAD_FAILURE_DUPLICATE_MESSAGE = "下载器已存在同一任务且本次无新增文件明细"
DOWNLOAD_FAILURE_RESOURCE_ERROR_KEYWORDS = (
    "无法读取种子文件",
    "无法获取下载地址",
    "种子下载失败",
    "种子数据有误",
    "下载失败黑名单",
    "torrent not found",
    "not found",
    "404",
    "deleted",
    "invalid torrent",
    "专辑资源",
)


class DownloadFailureOwner(_DownloadOwnerBase):
    """
    下载资源级失败与退避指纹、冷却和持久化 owner。
    """

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
    def _build_download_failure_fingerprint(
            cls, context: Context, *, resource_backoff: bool = False,
    ) -> Optional[str]:
        """
        根据媒体和种子资源信息生成冷却指纹，整包退避不依赖解析后补齐的集数。

        :param context: 识别出的媒体与资源上下文
        :param resource_backoff: 是否为整包均已记录的资源退避；与普通失败指纹隔离
        :return: 稳定资源指纹；缺少身份时返回 None
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
            "episodes": None if resource_backoff else cls._format_failure_episodes(meta) or "",
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
        限流按站点窗口冷却；泛化的空内容不能证明资源失效，使用瞬时冷却。
        """
        rate_limit_ttl = torrent_rules.rate_limit_cooldown(error_msg)
        if rate_limit_ttl is not None:
            return rate_limit_ttl
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
            ttl_seconds: Optional[int] = None,
            resource_backoff: bool = False,
    ) -> Optional[str]:
        """
        记录资源级冷却事实，并返回本次资源指纹。

        该台账是资源级退避记录，既承载真实失败，也承载"重复提交但无新增交付"这类
        没有失败、但不值得立刻再试一次的事实，因此 ``error_msg`` 只是跳过原因说明。

        :param context: 本次提交的上下文，用于计算资源指纹
        :param error_msg: 写入台账的跳过原因说明
        :param downloader: 本次使用的下载器
        :param source: 下载来源，仅订阅来源的冷却会被读取
        :param episodes: 目标集数，缺省时按识别元数据记录
        :param ttl_seconds: 显式冷却秒数，缺省时按原因文本推断
        :param resource_backoff: 是否使用不含运行时集数的整包退避指纹
        :return: 本次记录的资源指纹；缺少身份时返回 None
        """
        fingerprint = self._build_download_failure_fingerprint(context, resource_backoff=resource_backoff)
        if not fingerprint:
            return None

        now_timestamp = time.time()
        now_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_timestamp))
        next_retry_at = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(now_timestamp + (
                ttl_seconds if ttl_seconds is not None else self._download_failure_ttl(error_msg)
            )),
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

    def _record_duplicate_download_backoff(
            self,
            context: Context,
            downloader: Optional[str] = None,
            source: Optional[str] = None,
            episodes: Optional[Set[int]] = None,
    ) -> Optional[str]:
        """
        为重复接受且未带来任何新增文件的资源记录退避，并返回该资源指纹。

        调用方须确认整包全部媒体文件均已记录。退避只压制这一个资源，订阅候选会继续
        尝试同媒体的其他资源，订阅本身既不会被判定完成也不会被判失败，冷却到期后仍可重试。
        仅订阅来源需要记录，因为候选过滤只在订阅来源读取该台账。

        :param context: 本次结算的上下文，用于计算资源指纹
        :param downloader: 本次使用的下载器
        :param source: 下载来源
        :param episodes: 目标集数
        :return: 本次记录的整包资源指纹；非订阅来源返回 None
        """
        if not self._is_subscribe_source(source):
            return None
        return self._record_download_failure(
            context=context,
            error_msg=DOWNLOAD_FAILURE_DUPLICATE_MESSAGE,
            downloader=downloader,
            source=source,
            episodes=episodes,
            ttl_seconds=DOWNLOAD_FAILURE_DUPLICATE_TTL_SECONDS,
            resource_backoff=True,
        )

    def _active_download_failure_fingerprints(
            self,
            contexts: List[Context],
            source: Optional[str],
    ) -> Dict[str, DownloadFailureSnapshot]:
        """
        查询候选的普通失败与整包退避，并映射回当前候选指纹供所有选源阶段消费。

        :param contexts: 本轮待筛选的资源上下文
        :param source: 本次批量下载的业务来源
        :return: 当前候选指纹到有效冷却记录的映射；非订阅来源不读取台账
        """
        if not self._is_subscribe_source(source):
            return {}
        backoff_fingerprints = {
            fingerprint: self._build_download_failure_fingerprint(context, resource_backoff=True) or fingerprint
            for context in contexts or []
            if (fingerprint := self._build_download_failure_fingerprint(context))
        }
        fingerprints = list(dict.fromkeys([
            *backoff_fingerprints,
            *backoff_fingerprints.values(),
        ]))
        if not fingerprints:
            return {}
        now_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        try:
            repository: DownloadFailureRepository = self.download_failure_repository
            records = repository.get_active_by_fingerprints(
                fingerprints=fingerprints, now_time=now_time,
            )
            return {
                fingerprint: records.get(fingerprint) or records[backoff]
                for fingerprint, backoff in backoff_fingerprints.items()
                if fingerprint in records or backoff in records
            }
        except Exception as err:
            logger.error(f"查询下载失败冷却失败：{str(err)}")
            return {}

    @staticmethod
    def _log_download_failure_cooldown(
            context: Context,
            failure: Optional[DownloadFailureSnapshot],
    ) -> None:
        """
        记录候选资源处于冷却期时的跳过原因和下次重试时间。

        台账里的成因可能是真实失败，也可能是重复提交退避，所以这里只转述记录中的原因，
        不把它断言成"添加下载失败"。
        """
        torrent_title = context.torrent_info.title if context.torrent_info else "未知资源"
        reason = getattr(failure, "error_message", None) or "未知原因"
        retry_at = getattr(failure, "next_retry_at", None)
        if retry_at:
            logger.info(
                f"{torrent_title} 暂时跳过该资源（原因：{reason}），"
                f"将于 {retry_at} 后重试，本次继续尝试其他资源"
            )
        else:
            logger.info(
                f"{torrent_title} 暂时跳过该资源（原因：{reason}），"
                "本次继续尝试其他资源"
            )
