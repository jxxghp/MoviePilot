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
from app.schemas.download import ExistingTaskRejection
from app.schemas.media import build_media_key, resolve_media_identity

DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS = 24 * 60 * 60
DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS = 60 * 60
# 重复提交退避只压制单个订阅对单个资源的重投，必须长于订阅重新选源的周期，否则同一个整包仍会被
# 反复接受。登记条件见 DownloadHistoryOwner._should_record_duplicate_backoff。
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
    def _subscription_scope(source: Optional[str]) -> str:
        """
        从订阅来源中取出订阅 ID，作为重复提交退避的作用范围。

        订阅来源格式为 ``Subscribe|{JSON}``，JSON 中的名称等字段可能被修改，只取稳定的 ``id``；
        无法解析时退回完整来源文本，保证仍按来源隔离。

        :param source: 下载来源
        :return: 订阅范围标识；非订阅来源返回空字符串
        """
        if not source or not str(source).startswith("Subscribe|"):
            return ""
        try:
            subscription_id = json.loads(str(source).split("|", 1)[1]).get("id")
        except (ValueError, AttributeError):
            subscription_id = None
        return f"subscribe:{subscription_id}" if subscription_id is not None else str(source)

    @staticmethod
    def _download_scope(downloader: Optional[str], torrent: Optional[TorrentInfo]) -> str:
        """
        本次请求指向的下载器配置，作为已有任务拒绝的通知去重与重复提交退避的作用范围。

        选源阶段还不知道下载器实际选用了哪个实例，只能用"调用方指定的下载器，否则站点配置的下载器"表示；
        提交与记录时必须用同一口径，记录才能命中。两者都没有时为空字符串，代表当时的默认下载器
        （默认下载器在记录有效期内被改成别的实例属于已知近似，最多让一次重复退避多挡或少挡一次）。

        :param downloader: 调用方指定的下载器配置名称
        :param torrent: 候选种子，读取站点配置的下载器
        :return: 下载器范围标识
        """
        return str(downloader or getattr(torrent, "site_downloader", None) or "")

    @classmethod
    def _build_existing_task_fingerprint(
            cls, context: Context, *, downloader_scope: str, episodes: Optional[Set[int]] = None,
            target_specific: bool = False,
    ) -> Optional[str]:
        """
        生成"下载器中已有同一种子任务、但不能受理"的记录指纹，与普通失败指纹隔离。

        这类记录只用于"同一原因持续期间不重复通知"，不会让订阅跳过候选（见
        ``_existing_task_rejection_noted``），因此范围要与拒绝原因的实际范围一致，避免把别的情况当成重复：
        - 针对整个任务的原因（暂停、出错、读取或写入未确认）：范围是"下载器 + 种子"；
        - 只针对本次目标的原因（目标集找不到、目标文件被取消）：范围再加上本次目标，包括下载器选集实际使用的
          目标季与季集映射，多季包里 S01E03 与 S02E03 是不同的目标。
        原因归类见 ``ExistingTaskRejection.is_target_specific``。

        :param context: 本次提交的上下文，读取站点、种子资源键与目标季集依据
        :param downloader_scope: 下载器范围，见 ``_download_scope``
        :param episodes: 本次目标集号；None 或空集合表示整个种子，仅 ``target_specific`` 时使用
        :param target_specific: 是否为只针对本次目标的原因
        :return: 记录指纹；缺少种子资源键时返回 None
        """
        torrent = getattr(context, "torrent_info", None)
        resource = cls._torrent_resource_key(torrent)
        if not resource:
            return None
        site = getattr(torrent, "site", None) or getattr(torrent, "site_name", None)
        payload: Dict[str, object] = {
            "kind": "existing_task",
            "downloader": downloader_scope,
            "site": str(site or ""),
            "resource": resource,
        }
        if target_specific:
            meta = getattr(context, "meta_info", None)
            selection = (
                torrent_rules.episode_selection_for_context(context)
                if episodes and meta is not None and torrent is not None else None
            )
            meta_season = getattr(meta, "season", None)
            payload["episodes"] = sorted(episodes) if episodes else "whole"
            payload["season"] = selection.season if selection else str(meta_season or "")
            payload["episode_map"] = [list(pair) for pair in selection.episode_map] if selection else []
        raw_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

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
            cls, context: Context, *, resource_backoff: bool = False, source: Optional[str] = None,
            downloader_scope: str = "",
    ) -> Optional[str]:
        """
        根据媒体和种子资源信息生成冷却指纹，整包退避不依赖解析后补齐的集数。

        重复提交退避的指纹额外包含订阅 ID 与下载器范围：退避只表达"这个订阅往这台下载器反复重投该资源
        没有进展"，不能挡住其他订阅（例如试看后新建的正式订阅）对同一季包的补集请求；订阅改用另一台
        下载器后，同一资源会在那台下载器上新建任务，也不应被挡住。这两个字段同时让旧版本按
        "数据库明细已全部记录"写入、未经下载器核验的整包退避记录自然失效。

        :param context: 识别出的媒体与资源上下文
        :param resource_backoff: 是否为重复提交退避；与普通失败指纹隔离
        :param source: 下载来源，仅重复提交退避使用其中的订阅 ID
        :param downloader_scope: 下载器范围（见 ``_download_scope``），仅重复提交退避使用
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
        if resource_backoff:
            payload["subscription"] = cls._subscription_scope(source)
            payload["downloader"] = downloader_scope
        if not payload["media_type"] or not payload["media_key"] or not payload["resource"]:
            return None
        raw_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    @staticmethod
    def _download_failure_ttl(error_msg: Optional[str]) -> int:
        """
        限流按站点窗口冷却；已有任务的拒绝按原因区分资源级与瞬时冷却；换票、临时地址和泛化空内容
        失败使用瞬时冷却。

        :param error_msg: 准备或下载器提交阶段保留的具体失败原因
        :return: 冷却秒数；换票诊断中的 404 等关键字不能升级为资源失效
        """
        rate_limit_ttl = torrent_rules.rate_limit_cooldown(error_msg)
        if rate_limit_ttl is not None:
            return rate_limit_ttl
        # 已有任务的拒绝原因由下载器以固定开头返回，按原因归类，不从细节文本里匹配关键字
        # （细节里会出现"已删除""不存在"等字样，不能误判为资源失效）。
        rejection = ExistingTaskRejection.from_message(error_msg)
        if rejection is not None:
            if rejection.is_resource_level:
                return DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS
            return DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS
        error_text = str(error_msg or "").lower()
        if error_text.startswith(("换票失败：", "临时下载地址失效：")):
            return DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS
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
            downloader_scope: Optional[str] = None,
    ) -> Optional[str]:
        """
        记录资源级冷却事实，并返回本次资源指纹。

        该台账是资源级退避记录，既承载真实失败，也承载"重复提交但无新增交付"这类
        没有失败、但不值得立刻再试一次的事实，因此 ``error_msg`` 只是跳过原因说明。
        下载器以 ``ExistingTaskRejection`` 开头拒绝的已有任务，按已有任务指纹记录，只用于通知去重，
        见 ``_build_existing_task_fingerprint``。

        :param context: 本次提交的上下文，用于计算资源指纹
        :param error_msg: 写入台账的跳过原因说明
        :param downloader: 本次使用的下载器；拒绝时下载器不返回实际实例，即为请求指向的下载器
        :param source: 下载来源，仅订阅来源的冷却会被读取
        :param episodes: 目标集数，缺省时按识别元数据记录
        :param ttl_seconds: 显式冷却秒数，缺省时按原因文本推断
        :param resource_backoff: 是否使用不含运行时集数的整包退避指纹
        :param downloader_scope: 指纹使用的下载器范围（见 ``_download_scope``）；缺省时取 ``downloader``
        :return: 本次记录的资源指纹；缺少身份时返回 None
        """
        scope = downloader_scope if downloader_scope is not None else str(downloader or "")
        rejection = None if resource_backoff else ExistingTaskRejection.from_message(error_msg)
        if rejection is not None:
            fingerprint = self._build_existing_task_fingerprint(
                context, downloader_scope=scope, episodes=episodes, target_specific=rejection.is_target_specific,
            )
        else:
            fingerprint = self._build_download_failure_fingerprint(
                context, resource_backoff=resource_backoff, source=source, downloader_scope=scope,
            )
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
            downloader_scope: str = "",
    ) -> Optional[str]:
        """
        为订阅重复接受且未带来任何新增文件的资源记录退避，并返回该资源指纹。

        登记条件由 ``DownloadHistoryOwner._should_record_duplicate_backoff`` 判断。退避只压制
        这一个订阅对这一台下载器重投这一个资源，订阅候选会继续尝试同媒体的其他资源，其他订阅、
        其他下载器不受影响；订阅本身既不会被判定完成也不会被判失败，冷却到期后仍可重试。
        仅订阅来源需要记录，因为候选过滤只在订阅来源读取该台账。

        :param context: 本次结算的上下文，用于计算资源指纹
        :param downloader: 本次实际使用的下载器，写入台账供展示
        :param source: 下载来源
        :param episodes: 目标集数
        :param downloader_scope: 请求指向的下载器范围（见 ``_download_scope``），写入指纹；选源阶段只知道这一口径
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
            downloader_scope=downloader_scope,
        )

    def _active_download_failure_fingerprints(
            self,
            contexts: List[Context],
            source: Optional[str],
            downloader: Optional[str] = None,
    ) -> Dict[str, DownloadFailureSnapshot]:
        """
        查询候选的普通失败与整包退避，并映射回当前候选指纹供所有选源阶段消费。

        已有任务的拒绝记录不在这里拦截候选：任务状态随时可能被用户或下载器改变（恢复暂停、重新勾选文件、
        删除任务），每一轮都要交给下载器按实时状态重新判断，见 ``_existing_task_rejection_noted``。

        :param contexts: 本轮待筛选的资源上下文
        :param source: 本次批量下载的业务来源
        :param downloader: 本次批量下载指定的下载器，用于计算下载器范围
        :return: 当前候选指纹到有效冷却记录的映射；非订阅来源不读取台账
        """
        if not self._is_subscribe_source(source):
            return {}
        related: Dict[str, List[str]] = {}
        for context in contexts or []:
            fingerprint = self._build_download_failure_fingerprint(context)
            if not fingerprint:
                continue
            scope = self._download_scope(downloader, getattr(context, "torrent_info", None))
            related[fingerprint] = [
                item for item in (
                    self._build_download_failure_fingerprint(
                        context, resource_backoff=True, source=source, downloader_scope=scope,
                    ),
                ) if item
            ]
        fingerprints = list(dict.fromkeys(
            item for fingerprint, extras in related.items() for item in (fingerprint, *extras)
        ))
        if not fingerprints:
            return {}
        now_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        try:
            repository: DownloadFailureRepository = self.download_failure_repository
            records = repository.get_active_by_fingerprints(
                fingerprints=fingerprints, now_time=now_time,
            )
        except Exception as err:
            logger.error(f"查询下载失败冷却失败：{str(err)}")
            return {}
        active: Dict[str, DownloadFailureSnapshot] = {}
        for fingerprint, extras in related.items():
            record = next((records[item] for item in (fingerprint, *extras) if item in records), None)
            if record is not None:
                active[fingerprint] = record
        return active

    def _existing_task_rejection_noted(
            self,
            context: Context,
            *,
            rejection: ExistingTaskRejection,
            downloader_scope: str,
            episodes: Optional[Set[int]],
    ) -> bool:
        """
        判断这次已有任务的拒绝是否与仍在有效期内的上一次记录相同，相同则不再通知、不顺延记录。

        已有任务的拒绝不让订阅跳过候选，每一轮都会重新交给下载器判断；工作流、插件等自动流程也会定时重复提交。
        如果原因一直没变（例如任务始终暂停），每次都通知会刷屏。这里只做去重：同一范围（见
        ``_build_existing_task_fingerprint``，不区分来源）在记录有效期内出现同一原因时返回 True；原因变了、记录过期
        或范围不同都会照常通知并重新记录。有效期沿用失败冷却时长（取消、找不到、出错 24 小时，其余 1 小时），
        到期后再通知一次。调用方只对自动流程调用；用户手动指定种子时每次都通知。

        :param context: 本次提交的上下文
        :param rejection: 本次拒绝原因
        :param downloader_scope: 下载器范围，见 ``_download_scope``
        :param episodes: 本次目标集号；None 或空集合表示整个种子
        :return: 与有效记录的原因相同时返回 True
        """
        fingerprint = self._build_existing_task_fingerprint(
            context, downloader_scope=downloader_scope, episodes=episodes,
            target_specific=rejection.is_target_specific,
        )
        if not fingerprint:
            return False
        now_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        try:
            repository: DownloadFailureRepository = self.download_failure_repository
            record = repository.get_active_by_fingerprints(fingerprints=[fingerprint], now_time=now_time).get(fingerprint)
        except Exception as err:
            logger.error(f"查询已有任务拒绝记录失败：{str(err)}")
            return False
        return record is not None and ExistingTaskRejection.from_message(record.error_message) is rejection

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
