"""
待确认下载提交的类型化应用契约与目标冲突判断规则。

当下载器已受理添加请求但任务确认超时时，下载链通过本模块定义的持久化契约保留待确认状态，
并在后续轮次按已知种子哈希重新核对，避免把状态未知误判为未提交而连续下载不同版本。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Protocol

from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType

if TYPE_CHECKING:
    from app.domain.context import Context

# 待确认下载的默认保护窗口（秒）。超过该窗口且下载器明确在线查不到对应任务时才释放保护，
# 避免因下载器偶发丢失内存队列而永久卡死订阅；期间一旦查到任务会立即转正并删除待确认记录。
DOWNLOAD_PENDING_CONFIRM_TTL_SECONDS = 2 * 60 * 60


@dataclass(frozen=True, slots=True)
class DownloadPendingWrite:
    """
    新增或更新一条待确认下载记录所需的完整持久化输入。

    :param pending_key: 单条待确认提交的稳定唯一键，用于并发幂等写入与核对删除
    :param target_key: 媒体级目标键，用于按电影、音乐或电视剧作品批量查询活跃待确认记录
    :param media_type: 媒体类型文本（电影、电视剧、音乐）
    :param title: 识别出的标准媒体标题
    :param year: 媒体发行年份；未知时为 None
    :param media_source: 标准媒体数据源，与 media_id 配对使用
    :param media_id: 当前数据源中的原生媒体标识
    :param season: 电视剧单季季号；电影、音乐或多季整包时为 None
    :param episodes: 本次提交覆盖的目标集号元组；电影、音乐或整季提交时为 None
    :param downloader: 受理或请求指向的下载器配置名称；空字符串表示默认下载器
    :param hashes: 由种子内容解析出的已知小写 v1/v2 哈希元组；磁力链无法解析时为空元组
    :param torrent_name: 种子原始标题，用于日志与通知展示
    :param source: 触发本次提交的业务来源（例如 ``Subscribe|{...}``）
    :param subscription_id: 订阅来源解析出的订阅主键；非订阅来源时为 None
    :param payload: 确认转正结算所需的脱敏快照，不得包含凭据、Cookie 或签名下载链接
    :param submitted_at: 首次进入待确认状态的时间文本
    :param last_checked_at: 最近一次核对下载器的时间文本；尚未复核时为 None
    :param expires_at: 保护窗口截止时间文本；超期且下载器明确在线未命中时才释放
    """

    pending_key: str
    target_key: str
    submitted_at: str
    expires_at: str
    media_type: Optional[str] = None
    title: Optional[str] = None
    year: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    season: Optional[int] = None
    episodes: Optional[tuple[int, ...]] = None
    downloader: Optional[str] = None
    hashes: tuple[str, ...] = ()
    torrent_name: Optional[str] = None
    source: Optional[str] = None
    subscription_id: Optional[int] = None
    payload: Optional[Mapping[str, Any]] = None
    last_checked_at: Optional[str] = None


@dataclass(frozen=True, slots=True)
class DownloadPendingSnapshot:
    """
    脱离数据库会话后供核对与防重判断使用的只读待确认记录快照。

    :param pending_key: 待确认提交的稳定唯一键
    :param target_key: 媒体级目标键
    :param media_type: 媒体类型文本
    :param title: 识别出的标准媒体标题
    :param year: 媒体发行年份
    :param media_source: 标准媒体数据源
    :param media_id: 原生媒体标识
    :param season: 电视剧单季季号；电影、音乐或多季整包时为 None
    :param episodes: 覆盖的目标集号元组；整季或非剧集为 None
    :param downloader: 受理或请求指向的下载器名称；空字符串表示默认下载器
    :param hashes: 已知小写 v1/v2 种子哈希元组
    :param torrent_name: 种子标题
    :param source: 触发本次提交的业务来源
    :param subscription_id: 关联订阅主键；非订阅来源为 None
    :param payload: 确认转正结算所需的脱敏快照
    :param submitted_at: 提交时间文本
    :param last_checked_at: 最近核对时间文本
    :param expires_at: 保护窗口截止时间文本
    """

    pending_key: str
    target_key: str
    submitted_at: str
    expires_at: str
    media_type: Optional[str] = None
    title: Optional[str] = None
    year: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    season: Optional[int] = None
    episodes: Optional[tuple[int, ...]] = None
    downloader: Optional[str] = None
    hashes: tuple[str, ...] = ()
    torrent_name: Optional[str] = None
    source: Optional[str] = None
    subscription_id: Optional[int] = None
    payload: Optional[Mapping[str, Any]] = None
    last_checked_at: Optional[str] = None

    @property
    def covered_seasons(self) -> tuple[int, ...]:
        """
        返回电视剧待确认提交覆盖的季号集合。

        单季提交返回 ``(season,)``；多季整包从脱敏快照中的 ``seasons`` 读取，缺失时返回空元组。

        :return: 覆盖的季号元组
        """
        if self.season is not None:
            return (self.season,)
        raw_seasons = self.payload.get("seasons") if isinstance(self.payload, Mapping) else None
        if isinstance(raw_seasons, (list, tuple)):
            return tuple(int(item) for item in raw_seasons if isinstance(item, int))
        return ()


@dataclass(frozen=True, slots=True)
class ConfirmedPendingDownload:
    """
    待确认提交在下载器核对确认并补记下载历史后返回的业务回执。

    供订阅链和批量下载据此收敛订阅进度或完成状态，无需在下载链反向导入订阅链。

    :param record: 已被确认转正的原待确认记录快照
    :param context: 按脱敏快照重建的最小下载上下文，供订阅完成与进度记账使用
    :param downloader: 下载器核对返回的实际下载器名称
    :param download_hash: 下载器中的实际任务哈希
    """

    record: DownloadPendingSnapshot
    context: "Context"
    downloader: Optional[str]
    download_hash: str


@dataclass(frozen=True, slots=True)
class PendingReconciliationResult:
    """
    一次待确认下载核对的聚合结果。

    :param confirmed: 本次已在下载器确认并完成下载历史结算的记录回执
    :param still_pending: 仍未确认（保护期内未查到或下载器查询出错）而继续拦截同目标的记录
    :param released: 本次因明确拒绝或保护期满且下载器确认缺失而被释放的记录
    """

    confirmed: tuple[ConfirmedPendingDownload, ...] = ()
    still_pending: tuple[DownloadPendingSnapshot, ...] = ()
    released: tuple[DownloadPendingSnapshot, ...] = ()


class DownloadPendingRepository(Protocol):
    """
    下载链读写待确认提交状态所需的最小持久化端口。
    """

    def list_all(self) -> list[DownloadPendingSnapshot]:
        """
        查询当前全部待确认下载记录，供定时核对扫描使用。

        :return: 待确认记录快照列表，按提交顺序排列
        """
        ...

    def list_by_target_keys(self, target_keys: list[str]) -> list[DownloadPendingSnapshot]:
        """
        按媒体级目标键批量查询待确认下载记录。

        :param target_keys: 待检查的媒体目标键列表
        :return: 匹配的待确认记录快照列表
        """
        ...

    def list_by_subscription_id(self, subscription_id: int) -> list[DownloadPendingSnapshot]:
        """
        按订阅主键查询归属该订阅的待确认下载记录。

        :param subscription_id: 订阅主键
        :return: 匹配的待确认记录快照列表
        """
        ...

    def get_by_key(self, pending_key: str) -> Optional[DownloadPendingSnapshot]:
        """
        按唯一键读取单条待确认记录，用于持有任务锁后的二次确认。

        :param pending_key: 待确认记录唯一键
        :return: 记录快照；已被其他线程结算或删除时返回 None
        """
        ...

    def upsert(self, record: DownloadPendingWrite) -> None:
        """
        在独立短事务中新增或更新一条待确认记录。

        :param record: 待写入的待确认下载快照
        """
        ...

    def touch_checked(self, pending_key: str, checked_at: str) -> None:
        """
        记录一次未命中核对时间，不延长既定保护截止时间。

        :param pending_key: 待确认记录唯一键
        :param checked_at: 本次核对时间文本
        """
        ...

    def delete_by_key(self, pending_key: str) -> int:
        """
        按唯一键删除已确认转正或已释放的待确认记录。

        :param pending_key: 待确认记录唯一键
        :return: 删除的行数
        """
        ...

    def delete_by_target_key(self, target_key: str) -> int:
        """
        按媒体目标键清理待确认记录（例如目标已确认入库）。

        :param target_key: 媒体级目标键
        :return: 删除的行数
        """
        ...


def pending_blocks_movie_music(
    records: Iterable[DownloadPendingSnapshot],
    target_key: str,
) -> Optional[DownloadPendingSnapshot]:
    """
    判断电影或音乐目标是否存在活跃待确认记录。

    :param records: 当前仍在待确认状态的记录集合
    :param target_key: 候选媒体的目标键
    :return: 命中的首条待确认记录；不存在冲突时返回 None
    """
    if not target_key:
        return None
    for record in records:
        if record.target_key == target_key:
            return record
    return None


def pending_blocked_tv_scope(
    records: Iterable[DownloadPendingSnapshot],
    target_key: str,
    season: int,
) -> tuple[bool, frozenset[int]]:
    """
    汇总某部电视剧指定季被待确认提交占用的范围。

    :param records: 当前仍在待确认状态的记录集合
    :param target_key: 电视剧目标键
    :param season: 目标季号
    :return: ``(整季是否被占用, 被占用的离散集号集合)``
    """
    if not target_key:
        return False, frozenset()
    blocked_episodes: set[int] = set()
    for record in records:
        if record.target_key != target_key:
            continue
        covered_seasons = record.covered_seasons
        if covered_seasons and season not in covered_seasons:
            continue
        if not record.episodes:
            return True, frozenset()
        blocked_episodes.update(record.episodes)
    return False, frozenset(blocked_episodes)


def pending_blocks_tv_seasons(
    records: Iterable[DownloadPendingSnapshot],
    target_key: str,
    seasons: Iterable[int],
) -> bool:
    """
    判断整季或多季候选与活跃待确认记录是否存在任何季或集重叠。

    :param records: 当前仍在待确认状态的记录集合
    :param target_key: 电视剧目标键
    :param seasons: 整季候选覆盖的季号集合
    :return: 任一目标季存在整季或分集待确认提交时返回 True
    """
    for season in seasons:
        whole_blocked, blocked_episodes = pending_blocked_tv_scope(records, target_key, season)
        if whole_blocked or blocked_episodes:
            return True
    return False


def pending_targets_for_scan(
    records: Iterable[DownloadPendingSnapshot],
    target_key: str,
    media_type: Optional[MediaType],
    missing_seasons: Optional[Mapping[int, NotExistMediaInfo]] = None,
) -> set[str]:
    """
    计算分页搜索应停止继续翻页的待确认目标集合。

    仅返回已进入待确认的目标标识（格式与 ``SearchScan`` 的 ``targets`` 一致），
    供搜索扫描停止对同一目标继续翻深页，不会把订阅标记为已完成。

    :param records: 当前仍在待确认状态的记录集合
    :param target_key: 当前订阅媒体的目标键
    :param media_type: 当前订阅媒体类型
    :param missing_seasons: 电视剧当前缺失季映射；电影时忽略
    :return: 应当在本轮搜索中暂停翻页的目标标识集合
    """
    if not target_key:
        return set()
    if media_type == MediaType.MOVIE:
        return {"movie"} if pending_blocks_movie_music(records, target_key) else set()
    if media_type != MediaType.TV or not missing_seasons:
        return set()
    paused: set[str] = set()
    for season, info in missing_seasons.items():
        whole_blocked, blocked_episodes = pending_blocked_tv_scope(records, target_key, season)
        if whole_blocked:
            paused.add(f"{season}:season")
            if info.episodes:
                paused.update(f"{season}:{episode}" for episode in info.episodes)
            elif info.total_episode:
                start = info.start_episode or 1
                paused.update(f"{season}:{episode}" for episode in range(start, info.total_episode + 1))
        elif blocked_episodes:
            paused.update(f"{season}:{episode}" for episode in blocked_episodes)
            if not info.episodes:
                # 整季缺失中已有部分集待确认时，暂停继续深翻整季目标，避免重复补齐。
                paused.add(f"{season}:season")
    return paused
