"""订阅下载提交幂等身份与状态转换 owner。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, Set, cast

from app.application.download.admission import (
    DownloadReconciliationRequired,
    SubscriptionDownloadClaim,
    SubscriptionDownloadGovernance,
    SubscriptionDownloadRepository,
    SubscriptionDownloadRequest,
)
from app.chain.download.contract import _DownloadOwnerBase
from app.domain import episode as episode_rules
from app.domain.context import Context
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.types import MediaType


class DownloadAdmissionOwner(_DownloadOwnerBase):
    """计算订阅提交唯一键并通过持久账本控制外部副作用。"""

    def _build_subscription_download_request(
        self,
        *,
        context: Context,
        episodes: Optional[Set[int]],
        governance: SubscriptionDownloadGovernance,
    ) -> SubscriptionDownloadRequest:
        """组合订阅、torrent、季集覆盖与模式生成规范幂等请求。"""
        media = context.media_info
        meta = context.meta_info
        torrent = context.torrent_info
        media_source, media_id = resolve_media_identity(media=media)
        media_key = build_media_key(media_source, media_id) or (
            f"{getattr(media, 'title', '')}:{getattr(media, 'year', '')}"
        )
        seasons = sorted(set(getattr(meta, "season_list", None) or []))
        selected = sorted(set(episodes or getattr(meta, "episode_list", None) or []))
        if selected:
            coverage = f"episodes:{episode_rules.format_ranges(selected)}"
        elif seasons:
            coverage = "seasons:" + ",".join(str(season) for season in seasons) + ":full"
        else:
            coverage = "full"
        media_type = getattr(media, "type", None)
        media_type_value = getattr(media_type, "value", media_type)
        media_season = getattr(media, "season", None)
        meta_season = getattr(meta, "season", None)
        logical_identity = json.dumps(
            {
                "subscription_id": governance.subscription_id,
                "media_key": str(media_key or ""),
                "media_type": str(media_type_value or ""),
                "season": media_season if media_season is not None else meta_season,
                "episode_group": getattr(media, "episode_group", None),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        resource_key = self._torrent_resource_key(torrent)
        canonical = json.dumps(
            {
                "logical_identity": logical_identity,
                "resource_key": resource_key,
                "coverage": coverage,
                "mode": governance.mode,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return SubscriptionDownloadRequest(
            idempotency_key=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            subscription_id=governance.subscription_id,
            task_id=governance.task_id,
            logical_identity=logical_identity,
            resource_key=resource_key,
            coverage=coverage,
            mode=governance.mode,
        )

    def _claim_subscription_download(
        self,
        *,
        context: Context,
        episodes: Optional[Set[int]],
        governance: Optional[SubscriptionDownloadGovernance],
    ) -> tuple[Optional[SubscriptionDownloadClaim], Optional[str]]:
        """在下载器调用前认领唯一提交权，并返回已成功提交的历史 hash。"""
        if governance is None:
            return None, None
        legacy_hash = self._legacy_subscription_download_hash(
            context=context,
            episodes=episodes,
            governance=governance,
        )
        if legacy_hash:
            return None, legacy_hash
        repository = getattr(self, "subscription_download_repository", None)
        if repository is None:
            raise RuntimeError("订阅下载幂等仓储尚未配置")
        request = self._build_subscription_download_request(
            context=context,
            episodes=episodes,
            governance=governance,
        )
        claim = repository.claim(request)
        snapshot = claim.snapshot
        if claim.acquired:
            if not snapshot.attempt_token:
                raise RuntimeError("订阅下载提交已认领但缺少尝试令牌")
            return claim, None
        if snapshot.state == "succeeded" and snapshot.download_hash:
            return claim, snapshot.download_hash
        if snapshot.state in {"submitting", "accepted", "reconcile_required"}:
            raise DownloadReconciliationRequired(
                f"订阅下载提交 {snapshot.idempotency_key} 当前为 {snapshot.state}，需要先对账下载器"
            )
        return claim, None

    def _legacy_subscription_download_hash(
        self,
        *,
        context: Context,
        episodes: Optional[Set[int]],
        governance: SubscriptionDownloadGovernance,
    ) -> Optional[str]:
        """重读迁移前下载历史，兼容识别同订阅同 torrent 同覆盖的成功提交。"""
        media = context.media_info
        meta = context.meta_info
        torrent = context.torrent_info
        repository = getattr(self, "download_history_repository", None)
        media_source, media_id = resolve_media_identity(media=media)
        if repository is None or not media_source or not media_id or not torrent:
            return None
        histories = repository.get_by_media_identity(
            media_source=media_source,
            media_id=str(media_id),
            music_type=getattr(media, "music_type", None),
        )
        expected_episodes = episode_rules.format_ranges(
            sorted(set(episodes or getattr(meta, "episode_list", None) or []))
        )
        expected_season = str(getattr(meta, "season", None) or "")
        for history in histories:
            if not history.download_hash:
                continue
            if history.torrent_name != torrent.title or history.torrent_site != torrent.site_name:
                continue
            if not self._history_matches_subscription(history.note, governance.subscription_id):
                continue
            if getattr(history, "episode_group", None) != getattr(media, "episode_group", None):
                continue
            if expected_episodes:
                if history.episodes == expected_episodes:
                    return cast(str, history.download_hash)
                continue
            if getattr(media, "type", None) == MediaType.TV and str(history.seasons or "") != expected_season:
                continue
            return cast(str, history.download_hash)
        return None

    @staticmethod
    def _history_matches_subscription(note: object, subscription_id: int) -> bool:
        """从下载历史来源快照确认记录属于同一订阅，而非同媒体其他记录。"""
        if not isinstance(note, dict):
            return False
        source = note.get("source")
        if not isinstance(source, str) or not source.startswith("Subscribe|"):
            return False
        try:
            payload = json.loads(source.split("|", 1)[1])
            return int(payload.get("id")) == subscription_id
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return False

    @staticmethod
    def _subscription_download_cancelled(
        governance: Optional[SubscriptionDownloadGovernance],
    ) -> bool:
        """在可安全取消边界调用入口提供的取消检查。"""
        return bool(governance and governance.cancelled and governance.cancelled())

    @staticmethod
    def _subscription_download_retry_at(error: Optional[str], ttl_seconds: int) -> str:
        """把明确拒绝的下载提交推迟到失败冷却到期后。"""
        del error
        return (
            datetime.now(timezone.utc) + timedelta(seconds=max(1, ttl_seconds))
        ).isoformat(timespec="seconds")

    def _subscription_download_repository(self) -> SubscriptionDownloadRepository:
        """返回已配置的订阅下载仓储，缺失时拒绝越过外部副作用边界。"""
        repository = getattr(self, "subscription_download_repository", None)
        if repository is None:
            raise RuntimeError("订阅下载幂等仓储尚未配置")
        return cast(SubscriptionDownloadRepository, repository)
