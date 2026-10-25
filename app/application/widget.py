"""
复用既有查询和业务来源生成小组件最小快照；不执行业务写入或另设权限。
"""

import math
import time
import unicodedata
from collections.abc import Callable

from app.application.dashboard import DashboardQueryService
from app.application.history import HistoryQueryService
from app.application.query import DataQueryService
from app.application.security.user import UserSnapshot
from app.application.security.widget import WidgetGrant, available_widget_contents
from app.foundation.size import format_size
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.schemas.dashboard import DownloaderInfo, Storage
from app.schemas.query import QueryPageRequest, SubscriptionFilter
from app.schemas.transfer import DownloaderTorrent
from app.schemas.types import MediaType
from app.schemas.widget import (
    WidgetContent,
    WidgetIcon,
    WidgetItem,
    WidgetMetric,
    WidgetSection,
    WidgetSnapshot,
)

# 与 Swift 共享协议一致，单次最多六条，不随业务总数增加传输体积。
WIDGET_ITEM_LIMIT = 6
WIDGET_SNAPSHOT_MAX_BYTES = 32 * 1024
_TITLES: dict[WidgetContent, str] = {
    "overview": "实例概览",
    "downloads": "下载状态",
    "imports": "近期入库",
    "movieSubscriptions": "电影订阅",
    "tvSubscriptions": "剧集订阅",
    "musicSubscriptions": "音乐订阅",
}
_MEDIA_TYPES: dict[WidgetContent, MediaType] = {
    "movieSubscriptions": MediaType.MOVIE,
    "tvSubscriptions": MediaType.TV,
    "musicSubscriptions": MediaType.MUSIC,
}


def _text(value: str | None, maximum: int, fallback: str = "") -> str:
    """
    输出无控制字符的纯文本，按 UTF-8 字节限制且不截断多字节字符。

    :param value: 来源展示文字，不作为 HTML 或路径解释
    :param maximum: 共享协议允许的最大 UTF-8 字节数
    :param fallback: 空白或无有效字符时的固定替代文字
    :return: 可以安全进入原生展示模型的有界文本
    """
    clean = "".join(" " if unicodedata.category(char).startswith("C") else char for char in (value or ""))
    return clean.strip().encode("utf-8")[:maximum].decode("utf-8", errors="ignore") or fallback


def _metric(identity: str, title: str, value: str, icon: WidgetIcon) -> WidgetMetric:
    """
    构造服务端格式化指标，并限制 UTF-8 体积。

    :param identity: 服务端固定的指标标识
    :param title: 固定中文名称
    :param value: 已计算的展示值
    :param icon: 固定图标语义
    :return: 不含业务原始对象的指标
    """
    return WidgetMetric(id=identity, title=title, value=_text(value, 64, "—"), icon=icon)


class WidgetSnapshotService:
    """
    仅向最新授权内容投影有限摘要；各来源独立失败，不伪造零值或新鲜时间。
    """

    def __init__(
        self,
        *,
        data: DataQueryService,
        history: HistoryQueryService,
        dashboard: DashboardQueryService,
        downloads: Callable[[], list[DownloaderTorrent] | None],
        downloader: Callable[[], DownloaderInfo],
        storage: Callable[[], Storage],
        clock: Callable[[], float] = time.time,
    ) -> None:
        """
        注入原有查询与来源回调，异步请求不阻塞事件循环或共享 Session。

        :param data: 既有有界订阅查询，包含同作用域的真实总数
        :param history: 既有整理历史查询，保留成功筛选和数据库分页
        :param dashboard: 既有管理员媒体统计查询
        :param downloads: 原有下载链的采样回调，仅在工作线程内执行
        :param downloader: 共用下载器字节统计；只由管理员在工作线程内读取
        :param storage: 既有存储汇总的严格读取回调
        :param clock: 可信 UTC Unix 秒来源
        """
        self._data = data
        self._history = history
        self._dashboard = dashboard
        self._downloads = downloads
        self._downloader = downloader
        self._storage = storage
        self._clock = clock

    async def snapshot(self, grant: WidgetGrant) -> WidgetSnapshot:
        """
        依次读取最多六种授权内容，不让单个来源失败抹去其它成功结果。

        :param grant: 仅由专用授权服务生成的当前账号快照
        :return: 不超过 32 KiB 的最小协议模型；调用方返回前还须重验授权
        :raises ValueError: 响应无法满足有界协议或凭据已在采样期间到期
        """
        contents = available_widget_contents(grant.user)
        sections = []
        for content in contents:
            try:
                section = await self._section(content, grant.user)
            except Exception as error:
                # 不输出异常正文，供应方错误可能含地址、路径或请求凭据。
                logger.warning(f"小组件 {content} 来源暂不可用（{type(error).__name__}）")
                section = WidgetSection(
                    content=content, state="unavailable", title=_TITLES[content], message="暂时无法读取，请稍后刷新"
                )
            sections.append(section)
        generated_at = self._clock()
        expires_at = min(generated_at + 86400, grant.registration.expires_at)
        if expires_at <= generated_at:
            raise ValueError("小组件凭据已到期")
        snapshot = WidgetSnapshot(
            owner=grant.registration.owner,
            generatedAt=generated_at,
            expiresAt=expires_at,
            availableContents=contents,
            sections=sections,
        )
        if len(snapshot.model_dump_json(exclude_none=True).encode("utf-8")) > WIDGET_SNAPSHOT_MAX_BYTES:
            raise ValueError("小组件快照超过协议体积")
        return snapshot

    async def _section(self, content: WidgetContent, user: UserSnapshot) -> WidgetSection:
        """
        只选择服务端固定来源；客户端不能传入任意查询、类型或 URL。
        """
        if content == "overview":
            return await self._overview()
        if content == "downloads":
            return await self._download_section(user)
        if content == "imports":
            return await self._imports()
        return await self._subscriptions(content, user)

    def _ready(
        self,
        content: WidgetContent,
        total: int,
        items: list[WidgetItem],
        *,
        icon: WidgetIcon = "items",
    ) -> WidgetSection:
        """
        使用真实查询总数确定空状态，列表截断不影响计数。
        """
        return WidgetSection(
            content=content,
            state="ready" if total else "empty",
            updatedAt=self._clock(),
            title=_TITLES[content],
            metrics=[_metric("count", "数量", f"{total:,}", icon)],
            items=items,
        )

    async def _subscriptions(self, content: WidgetContent, user: UserSnapshot) -> WidgetSection:
        """
        复用有界订阅查询的 owner、媒体类型、排序和精确 count，不扫描全量订阅。
        """
        page = await self._data.async_list_subscriptions(
            filters=SubscriptionFilter(
                usernames=() if user.is_superuser else (user.name,), media_types=(_MEDIA_TYPES[content],)
            ),
            page=QueryPageRequest(count=WIDGET_ITEM_LIMIT),
        )
        items = [
            WidgetItem(
                id=f"subscription-{row.id}",
                title=_text(row.name, 240, "未命名订阅"),
                subtitle=_text(" · ".join(filter(None, [row.year, row.type, row.state])), 512),
            )
            for row in page.items
        ]
        return self._ready(content, page.total, items)

    async def _imports(self) -> WidgetSection:
        """
        使用原历史查询只取最近六条成功入库，避免失败记录进入已入库组件。
        """
        page = await self._history.list_transfer(page=1, count=WIDGET_ITEM_LIMIT, status=True)
        items = [
            WidgetItem(
                id=f"import-{row.id}",
                title=_text(row.title, 240, "未命名媒体"),
                subtitle=_text(" · ".join(filter(None, [row.year, row.type, row.seasons, row.episodes])), 512),
            )
            for row in page.list
        ]
        return self._ready("imports", page.total, items)

    async def _download_section(self, user: UserSnapshot) -> WidgetSection:
        """
        按原网页精确 owner 过滤任务，仅管理员读取全局速率与本地目录空间。

        :param user: 本次重新鉴权的用户与既有权限快照
        :return: 最多六项任务及同作用域的真实计数，管理员附加三项全局指标
        :raises ValueError: 任务或统计来源暂不可用，或原始进度超出自然范围
        """
        rows, info = await run_in_threadpool(self._download_sources, user.is_superuser)
        visible = (
            rows if user.is_superuser else [row for row in rows if row.userid == user.name or row.username == user.name]
        )
        items = []
        for index, row in enumerate(visible[:WIDGET_ITEM_LIMIT]):
            progress = row.progress
            if progress is not None and (not math.isfinite(progress) or not 0 <= progress <= 100):
                raise ValueError("下载进度超出来源范围")
            items.append(
                WidgetItem(
                    id=f"download-{index}",
                    title=_text(row.title or row.name, 240, "未命名下载"),
                    subtitle=_text(" · ".join(filter(None, [row.dlspeed, row.left_time])), 512),
                    progress=progress / 100 if progress is not None else None,
                )
            )
        section = self._ready("downloads", len(visible), items, icon="download")
        if info is not None:
            section.metrics = self._download_metrics(info) + section.metrics
            # 无下载任务仍可正在做种上传，有效速率不能被空列表状态隐藏。
            section.state = "ready"
        return section

    def _download_sources(self, administrator: bool) -> tuple[list[DownloaderTorrent], DownloaderInfo | None]:
        """
        在同一个受管工作线程采样任务与统计，普通用户不触发全局读取。

        :param administrator: 当前重新鉴权用户是否为超级管理员
        :return: 真实任务列表与仅管理员可读的原始下载器统计
        :raises ValueError: 原下载任务来源尚不可用
        """
        rows = self._downloads()
        if rows is None:
            raise ValueError("下载来源暂不可用")
        return rows, self._downloader() if administrator else None

    def _download_metrics(self, info: DownloaderInfo) -> list[WidgetMetric]:
        """
        投影管理员全局速率与剩余空间，未知容量和真实磁盘满明确区分。

        :param info: 既有下载器的原始字节采样，不从任务展示文字重算
        :return: 固定标识的上传、下载和空间指标
        :raises ValueError: 速度缺失、负数或非有限；不能伪造为成功的零值
        """
        download, upload, available = info.download_speed, info.upload_speed, info.free_space
        if download is None or upload is None or not all(
            math.isfinite(value) and value >= 0 for value in (download, upload)
        ):
            raise ValueError("下载器速度来源暂不可用")
        free_space = (
            format_size(int(available))
            if available is not None and math.isfinite(available) and available >= 0 else "—"
        )
        return [
            _metric("downloadSpeed", "下载速度", f"{format_size(int(download))}/s", "download"),
            _metric("uploadSpeed", "上传速度", f"{format_size(int(upload))}/s", "upload"),
            _metric("freeSpace", "剩余空间", free_space, "storage"),
        ]

    async def _overview(self) -> WidgetSection:
        """
        汇总原仪表板媒体与存储；严格来源失败不能投影为成功的零容量。
        """
        statistic = await run_in_threadpool(self._dashboard.statistic, require_source=True)
        storage = await run_in_threadpool(self._storage)
        total, used = storage.total_storage, storage.used_storage
        if (
            total is None
            or used is None
            or not math.isfinite(total)
            or not math.isfinite(used)
            or not 0 <= used <= total
        ):
            raise ValueError("存储统计超出来源范围")
        return WidgetSection(
            content="overview",
            state="ready",
            updatedAt=self._clock(),
            title=_TITLES["overview"],
            metrics=[
                _metric("movies", "电影", str(statistic.movie_count), "movie"),
                _metric("tv", "电视剧", str(statistic.tv_count), "television"),
                _metric(
                    "episodes",
                    "单集",
                    str(statistic.episode_count) if statistic.episode_count is not None else "—",
                    "episodes",
                ),
                _metric("music", "音乐", str(statistic.music_count), "music"),
                _metric("used", "已用空间", format_size(int(used)), "storage"),
                _metric("total", "总空间", format_size(int(total)), "storage"),
            ],
        )
