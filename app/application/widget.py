"""
复用既有查询和业务来源生成小组件最小快照；不执行业务写入或另设权限。
"""

import math
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any, Literal

from app.application.history import HistoryQueryService
from app.application.query import DataQueryService
from app.application.security.user import UserSnapshot
from app.application.security.widget import (
    WidgetAccessError,
    WidgetGrant,
    available_widget_contents,
    widget_subscription_features,
)
from app.foundation.size import format_size
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.schemas.dashboard import DashboardMemoryInfo, DownloaderInfo
from app.schemas.query import QueryPageRequest, SubscriptionFilter, SubscriptionSnapshot, TransferHistoryFilter
from app.schemas.subscribe import Subscribe
from app.schemas.transfer import DownloaderTorrent
from app.schemas.types import MediaType
from app.schemas.widget import (
    PluginWidgetCatalog,
    PluginWidgetDescriptor,
    PluginWidgetKind,
    PluginWidgetListItem,
    PluginWidgetMetricItem,
    PluginWidgetSnapshot,
    PluginWidgetTone,
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
# 插件声明式自定义布局节点树的最大递归深度与单卡片最大节点总数。
PLUGIN_WIDGET_MAX_DEPTH = 5
PLUGIN_WIDGET_MAX_NODES = 40
_PLUGIN_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}\Z")
_WIDGET_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{0,64}\Z")
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}\Z")
_ALLOWED_KINDS: frozenset[str] = frozenset({"metrics", "list", "poster", "nowPlaying", "custom"})
_ALLOWED_TONES: frozenset[str] = frozenset(
    {"default", "primary", "secondary", "muted", "accent", "success", "warning", "danger"}
)
_ALLOWED_NODE_TYPES: frozenset[str] = frozenset(
    {"column", "row", "stack", "box", "text", "icon", "image", "progress", "badge", "button", "spacer", "divider"}
)
_TITLES: dict[WidgetContent, str] = {
    "overview": "实例概览",
    "downloads": "下载状态",
    "imports": "近期入库",
    "subscriptions": "订阅",
    "movieSubscriptions": "电影订阅",
    "tvSubscriptions": "剧集订阅",
    "musicSubscriptions": "音乐订阅",
}
_MEDIA_TYPES: dict[WidgetContent, MediaType] = {
    "movieSubscriptions": MediaType.MOVIE,
    "tvSubscriptions": MediaType.TV,
    "musicSubscriptions": MediaType.MUSIC,
}
_FEATURE_TYPES = {
    "subscribe.movie": MediaType.MOVIE, "subscribe.tv": MediaType.TV, "subscribe.music": MediaType.MUSIC,
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
        system: Callable[[], tuple[float, DashboardMemoryInfo, list[int]]],
        downloads: Callable[[], list[DownloaderTorrent] | None],
        downloader: Callable[[], DownloaderInfo],
        plugin_widgets: Callable[[], list[dict[str, Any]]] | None = None,
        plugin_widget_data: Callable[[str, str], dict[str, Any] | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """
        注入原有查询与来源回调，异步请求不阻塞事件循环或共享 Session。

        :param data: 既有有界订阅查询，包含同作用域的真实总数
        :param history: 既有整理历史查询，保留成功筛选和数据库分页
        :param system: 既有管理员 CPU、内存和网络系统采样回调
        :param downloads: 原有下载链的采样回调，仅在工作线程内执行
        :param downloader: 共用下载器字节统计；只由管理员在工作线程内读取
        :param plugin_widgets: 可选的运行态插件小组件元信息回调，仅超级管理员可调用
        :param plugin_widget_data: 可选的运行态插件单卡片数据回调，仅超级管理员在工作线程内读取
        :param clock: 可信 UTC Unix 秒来源
        """
        self._data = data
        self._history = history
        self._system = system
        self._downloads = downloads
        self._downloader = downloader
        self._plugin_widgets = plugin_widgets
        self._plugin_widget_data = plugin_widget_data
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
        media_types = (
            tuple(_FEATURE_TYPES[feature] for feature in widget_subscription_features(user))
            if content == "subscriptions" else (_MEDIA_TYPES[content],)
        )
        filters = SubscriptionFilter(usernames=() if user.is_superuser else (user.name,), media_types=media_types)
        page = await self._data.async_list_subscriptions(
            filters=filters,
            page=QueryPageRequest(count=WIDGET_ITEM_LIMIT),
        )
        section = self._ready(content, page.total, [self._subscription_item(row) for row in page.items])
        for kind in media_types:
            counted = await self._data.async_list_subscriptions(
                filters=filters.model_copy(update={"media_types": (kind,)}), page=QueryPageRequest(count=1)
            )
            section.metrics.append(_metric(f"type{kind.name}", kind.value, f"{counted.total:,}", "items"))
        for state, title in (("N", "未开始"), ("R", "订阅中"), ("S", "已暂停"), ("P", "待定")):
            counted = await self._data.async_list_subscriptions(
                filters=filters.model_copy(update={"states": (state,)}),
                page=QueryPageRequest(count=1),
            )
            section.metrics.append(_metric(f"state{state}", title, f"{counted.total:,}", "items"))
        return section

    @staticmethod
    def _subscription_item(row: SubscriptionSnapshot) -> WidgetItem:
        """
        沿用公开订阅响应的集数派生规则，保留季号、目标规格和可确认的进度。

        :param row: 已按当前账号和媒体类型过滤的有限快照
        :return: 不包含路径、规则或资源地址的媒体摘要
        """
        view = Subscribe.model_validate(row.model_dump(exclude_none=True))
        details = [row.type, row.year]
        if row.type == MediaType.TV.value and row.season is not None:
            details.append(f"S{row.season:02d}")
        progress = None
        if row.type == MediaType.TV.value and row.total_episode and view.completed_episode is not None:
            completed = min(max(view.completed_episode, 0), row.total_episode)
            details.append(f"{completed}/{row.total_episode} 集")
            progress = completed / row.total_episode
        elif row.type == MediaType.MUSIC.value and row.total_tracks:
            details.append(f"{row.total_tracks} 首曲目")
        details.extend([row.quality, row.resolution, row.audio_quality, row.audio_format])
        details.append({"N": "未开始", "R": "订阅中", "S": "已暂停", "P": "待定"}.get(row.state or "", "状态未知"))
        return WidgetItem(
            id=f"subscription-{row.id}", title=_text(row.name, 240, "未命名订阅"),
            subtitle=_text(" · ".join(filter(None, details)), 512), progress=progress,
        )

    async def _imports(self) -> WidgetSection:
        """
        使用原历史查询只取最近六条成功入库，避免失败记录进入已入库组件。
        """
        page = await self._history.list_transfer(page=1, count=WIDGET_ITEM_LIMIT, status=True)
        items = [
            WidgetItem(
                id=f"import-{row.id}",
                title=_text(row.title, 240, "未命名媒体"),
                subtitle=_text(" · ".join(filter(None, [row.year, row.type, row.seasons, row.episodes, row.date])), 512),
            )
            for row in page.list
        ]
        section = self._ready("imports", page.total, items)
        types: tuple[tuple[MediaType, WidgetIcon], ...] = (
            (MediaType.MOVIE, "movie"), (MediaType.TV, "television"), (MediaType.MUSIC, "music")
        )
        for media_type, icon in types:
            counted = await self._data.async_list_transfer_history(
                filters=TransferHistoryFilter(media_types=(media_type,), status=True), page=QueryPageRequest(count=1)
            )
            section.metrics.append(_metric(f"type{media_type.name}", media_type.value, f"{counted.total:,}", icon))
        return section

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
                    subtitle=_text(" · ".join(filter(None, [row.season_episode, row.dlspeed, row.left_time])), 512),
                    progress=progress / 100 if progress is not None else None,
                )
            )
        section = self._ready("downloads", len(visible), items, icon="download")
        if info is not None:
            section.metrics = self._download_metrics(info) + section.metrics
            # 无下载任务仍可正在做种上传，有效速率不能被空列表状态隐藏。
            section.state = "ready"
        for state, title in (("downloading", "下载中"), ("paused", "已暂停")):
            section.metrics.append(_metric(f"state{state}", title, str(sum(row.state == state for row in visible)), "items"))
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
        仅投影原管理员系统监控，不再把媒体数量放入系统概览。
        """
        cpu, memory, network = await run_in_threadpool(self._system)
        if (
            not math.isfinite(cpu) or not 0 <= cpu <= 100
            or memory.total <= 0 or not 0 <= memory.available <= memory.total
            or len(network) != 2 or any(not math.isfinite(value) or value < 0 for value in network)
        ):
            raise ValueError("系统采样超出来源范围")
        used = memory.total - memory.available
        return WidgetSection(
            content="overview",
            state="ready",
            updatedAt=self._clock(),
            title=_TITLES["overview"],
            metrics=[
                _metric("cpu", "CPU 占用", f"{cpu:.1f}%", "cpu"),
                _metric("memory", "内存占用", f"{used / memory.total * 100:.1f}%", "memory"),
                _metric("networkDownload", "网络接收", f"{format_size(network[1])}/s", "download"),
                _metric("networkUpload", "网络发送", f"{format_size(network[0])}/s", "upload"),
                _metric("memoryUsed", "已用内存", format_size(used), "memory"),
                _metric("memoryTotal", "总内存", format_size(memory.total), "memory"),
            ],
        )

    async def plugin_catalog(self, grant: WidgetGrant) -> PluginWidgetCatalog:
        """
        向拥有超级管理员权限的小组件授权返回可绑定的已启用插件小组件目录。

        :param grant: 已核验有效的专用只读授权
        :return: 不超过 32 KiB 的插件小组件目录模型
        :raises WidgetAccessError: 当前账号不具备管理员插件查看权限
        :raises ValueError: 凭据已到期或响应超出协议体积上限
        """
        if not grant.user.is_superuser:
            raise WidgetAccessError(403, "当前账号无权读取插件小组件")
        raw_items = (
            await run_in_threadpool(self._plugin_widgets)
            if self._plugin_widgets is not None
            else []
        )
        descriptors: list[PluginWidgetDescriptor] = []
        for raw in (raw_items or [])[:64]:
            if not isinstance(raw, Mapping):
                continue
            pid = str(raw.get("plugin_id") or "").strip()
            key = str(raw.get("key") or "").strip()
            if not _PLUGIN_ID_RE.fullmatch(pid) or not _WIDGET_KEY_RE.fullmatch(key):
                continue
            pname = _text(str(raw.get("plugin_name") or pid), 120, pid)
            wname = _text(str(raw.get("name") or pname), 120, pname)
            raw_desc = raw.get("description")
            desc = _text(str(raw_desc), 240) if isinstance(raw_desc, str) and raw_desc.strip() else None
            raw_kind = str(raw.get("kind") or "metrics").strip()
            kind: PluginWidgetKind = raw_kind if raw_kind in _ALLOWED_KINDS else "metrics"  # type: ignore[assignment]
            source: Literal["widget", "dashboard"] = "dashboard" if raw.get("source") == "dashboard" else "widget"
            descriptors.append(
                PluginWidgetDescriptor(
                    pluginId=pid,
                    pluginName=pname,
                    widgetKey=key,
                    name=wname,
                    description=desc,
                    kind=kind,
                    source=source,
                    hasPage=bool(raw.get("has_page")),
                )
            )
        generated_at = self._clock()
        expires_at = min(generated_at + 86400, grant.registration.expires_at)
        if expires_at <= generated_at:
            raise ValueError("小组件凭据已到期")
        catalog = PluginWidgetCatalog(
            owner=grant.registration.owner,
            generatedAt=generated_at,
            expiresAt=expires_at,
            widgets=descriptors,
        )
        if len(catalog.model_dump_json(exclude_none=True).encode("utf-8")) > WIDGET_SNAPSHOT_MAX_BYTES:
            raise ValueError("插件小组件目录超过协议体积")
        return catalog

    async def plugin_snapshot(
        self,
        grant: WidgetGrant,
        plugin_id: str,
        widget_key: str = "",
    ) -> PluginWidgetSnapshot:
        """
        读取单个插件小组件的有界只读快照，对预设字段和声明式自定义布局执行严格清洗。

        :param grant: 已核验有效的专用只读授权
        :param plugin_id: 目标运行态插件标识
        :param widget_key: 插件内小组件标识，缺省为空字符串
        :return: 不超过 32 KiB 的单卡片只读快照
        :raises WidgetAccessError: 权限不足或插件标识格式非法
        :raises ValueError: 凭据已到期或清洗后快照仍超出协议体积上限
        """
        if not grant.user.is_superuser:
            raise WidgetAccessError(403, "当前账号无权读取插件小组件")
        clean_pid = plugin_id.strip()
        clean_key = widget_key.strip()
        if not _PLUGIN_ID_RE.fullmatch(clean_pid) or not _WIDGET_KEY_RE.fullmatch(clean_key):
            raise WidgetAccessError(404, "目标插件小组件不存在")
        generated_at = self._clock()
        expires_at = min(generated_at + 86400, grant.registration.expires_at)
        if expires_at <= generated_at:
            raise ValueError("小组件凭据已到期")
        if self._plugin_widget_data is None:
            return self._unavailable_plugin_snapshot(grant, clean_pid, clean_key, generated_at, expires_at)
        try:
            raw = await run_in_threadpool(self._plugin_widget_data, clean_pid, clean_key)
        except Exception as error:
            logger.warning(f"插件小组件 {clean_pid}/{clean_key} 读取失败（{type(error).__name__}）")
            return self._unavailable_plugin_snapshot(grant, clean_pid, clean_key, generated_at, expires_at)
        if raw is None or not isinstance(raw, Mapping):
            return PluginWidgetSnapshot(
                owner=grant.registration.owner,
                generatedAt=generated_at,
                expiresAt=expires_at,
                pluginId=clean_pid,
                pluginName=clean_pid,
                widgetKey=clean_key,
                state="empty",
                updatedAt=generated_at,
                kind="metrics",
                title=clean_pid,
                message="当前暂无可展示的小组件数据",
            )
        return self._project_plugin_snapshot(grant, clean_pid, clean_key, raw, generated_at, expires_at)

    @staticmethod
    def _unavailable_plugin_snapshot(
        grant: WidgetGrant,
        plugin_id: str,
        widget_key: str,
        generated_at: float,
        expires_at: float,
    ) -> PluginWidgetSnapshot:
        """
        构造不包含内部异常细节或虚假采样时间的不可用插件小组件快照。
        """
        return PluginWidgetSnapshot(
            owner=grant.registration.owner,
            generatedAt=generated_at,
            expiresAt=expires_at,
            pluginId=plugin_id,
            pluginName=plugin_id,
            widgetKey=widget_key,
            state="unavailable",
            kind="metrics",
            title=plugin_id,
            message="暂时无法读取插件小组件，请稍后刷新",
        )

    def _project_plugin_snapshot(
        self,
        grant: WidgetGrant,
        plugin_id: str,
        widget_key: str,
        raw: Mapping[str, Any],
        generated_at: float,
        expires_at: float,
    ) -> PluginWidgetSnapshot:
        """
        将插件返回的原始映射规范化为受体积和层级约束的只读快照。
        """
        pname = _text(str(raw.get("plugin_name") or plugin_id), 120, plugin_id)
        title = _text(str(raw.get("title") or pname), 240, pname)
        raw_sub = raw.get("subtitle")
        subtitle = _text(str(raw_sub), 240) if isinstance(raw_sub, str) and raw_sub.strip() else None
        raw_kind = str(raw.get("kind") or "metrics").strip()
        kind: PluginWidgetKind = raw_kind if raw_kind in _ALLOWED_KINDS else "metrics"  # type: ignore[assignment]

        metrics = self._sanitize_plugin_metrics(raw.get("metrics"))
        items = self._sanitize_plugin_items(raw.get("items"))
        layout = self._sanitize_layout_root(raw.get("layout"))
        layouts_raw = raw.get("layouts")
        layouts: dict[str, dict[str, Any]] | None = None
        if isinstance(layouts_raw, Mapping):
            cleaned_layouts: dict[str, dict[str, Any]] = {}
            for size_key in ("small", "medium", "large"):
                sanitized_tree = self._sanitize_layout_root(layouts_raw.get(size_key))
                if sanitized_tree is not None:
                    cleaned_layouts[size_key] = sanitized_tree
            if cleaned_layouts:
                layouts = cleaned_layouts

        has_content = bool(metrics or items or layout or layouts or kind in {"poster", "nowPlaying"})
        snapshot = PluginWidgetSnapshot(
            owner=grant.registration.owner,
            generatedAt=generated_at,
            expiresAt=expires_at,
            pluginId=plugin_id,
            pluginName=pname,
            widgetKey=widget_key,
            state="ready" if has_content else "empty",
            updatedAt=generated_at,
            kind=kind,
            title=title,
            subtitle=subtitle,
            metrics=metrics,
            items=items,
            layout=layout,
            layouts=layouts,
        )
        if len(snapshot.model_dump_json(exclude_none=True).encode("utf-8")) > WIDGET_SNAPSHOT_MAX_BYTES:
            raise ValueError("插件小组件快照超过协议体积")
        return snapshot

    @staticmethod
    def _sanitize_plugin_metrics(raw_metrics: Any) -> list[PluginWidgetMetricItem]:
        """
        清洗预设指标数组，限制最多八项并剔除未知色调与控制字符。
        """
        if not isinstance(raw_metrics, list):
            return []
        out: list[PluginWidgetMetricItem] = []
        seen_ids: set[str] = set()
        for idx, entry in enumerate(raw_metrics[:8]):
            if not isinstance(entry, Mapping):
                continue
            mid = _text(str(entry.get("id") or f"m-{idx}"), 64, f"m-{idx}")
            if mid in seen_ids:
                mid = f"{mid}-{idx}"
            seen_ids.add(mid)
            label = _text(str(entry.get("label") or entry.get("title") or "指标"), 120, "指标")
            val = _text(str(entry.get("value") or "—"), 96, "—")
            raw_sub = entry.get("subValue")
            sub_val = _text(str(raw_sub), 120) if isinstance(raw_sub, str) and raw_sub.strip() else None
            raw_icon = entry.get("icon")
            icon = _text(str(raw_icon), 48) if isinstance(raw_icon, str) and raw_icon.strip() else None
            raw_tone = str(entry.get("tone") or "").strip()
            tone: PluginWidgetTone | None = raw_tone if raw_tone in _ALLOWED_TONES else None  # type: ignore[assignment]
            out.append(
                PluginWidgetMetricItem(
                    id=mid,
                    label=label,
                    value=val,
                    subValue=sub_val,
                    icon=icon,
                    tone=tone,
                )
            )
        return out

    @staticmethod
    def _sanitize_plugin_items(raw_items: Any) -> list[PluginWidgetListItem]:
        """
        清洗预设列表条目，限制最多六项并校验进度处于自然百分比区间。
        """
        if not isinstance(raw_items, list):
            return []
        out: list[PluginWidgetListItem] = []
        seen_ids: set[str] = set()
        for idx, entry in enumerate(raw_items[:WIDGET_ITEM_LIMIT]):
            if not isinstance(entry, Mapping):
                continue
            iid = _text(str(entry.get("id") or f"i-{idx}"), 64, f"i-{idx}")
            if iid in seen_ids:
                iid = f"{iid}-{idx}"
            seen_ids.add(iid)
            title = _text(str(entry.get("title") or "未命名条目"), 240, "未命名条目")
            raw_sub = entry.get("subtitle")
            subtitle = _text(str(raw_sub), 320) if isinstance(raw_sub, str) and raw_sub.strip() else None
            raw_trailing = entry.get("trailing")
            trailing = _text(str(raw_trailing), 64) if isinstance(raw_trailing, str) and raw_trailing.strip() else None
            raw_prog = entry.get("progress")
            progress: float | None = None
            if isinstance(raw_prog, (int, float)) and not isinstance(raw_prog, bool):
                prog_val = float(raw_prog)
                if math.isfinite(prog_val) and 0.0 <= prog_val <= 1.0:
                    progress = prog_val
            out.append(
                PluginWidgetListItem(
                    id=iid,
                    title=title,
                    subtitle=subtitle,
                    trailing=trailing,
                    progress=progress,
                )
            )
        return out

    @classmethod
    def _sanitize_layout_root(cls, raw_node: Any) -> dict[str, Any] | None:
        """
        对声明式自定义小组件节点树执行深度不超过五层、总节点不超过四十个的白名单清洗。
        """
        budget = [PLUGIN_WIDGET_MAX_NODES]
        return cls._sanitize_layout_node(raw_node, depth=0, budget=budget)

    @classmethod
    def _sanitize_layout_node(
        cls,
        raw_node: Any,
        *,
        depth: int,
        budget: list[int],
    ) -> dict[str, Any] | None:
        """
        递归校验单个声明式布局节点，剔除未知属性、外部任意地址与越界数值。
        """
        if depth > PLUGIN_WIDGET_MAX_DEPTH or budget[0] <= 0 or not isinstance(raw_node, Mapping):
            return None
        node_type = str(raw_node.get("type") or "").strip()
        if node_type not in _ALLOWED_NODE_TYPES:
            return None
        budget[0] -= 1
        out: dict[str, Any] = {"type": node_type}

        for str_key, max_bytes in (
            ("text", 240),
            ("label", 120),
            ("name", 64),
            ("size", 16),
            ("weight", 16),
            ("align", 16),
            ("justify", 24),
            ("style", 16),
        ):
            val = raw_node.get(str_key)
            if isinstance(val, str) and val.strip():
                out[str_key] = _text(val, max_bytes)

        raw_tone = raw_node.get("tone")
        if isinstance(raw_tone, str):
            clean_tone = raw_tone.strip()
            if clean_tone in _ALLOWED_TONES or _HEX_COLOR_RE.fullmatch(clean_tone):
                out["tone"] = clean_tone

        for num_key, low, high in (
            ("gap", 0.0, 32.0),
            ("padding", 0.0, 32.0),
            ("radius", 0.0, 32.0),
            ("width", 8.0, 360.0),
            ("height", 8.0, 360.0),
            ("flex", 0.0, 10.0),
            ("maxLines", 1.0, 6.0),
            ("value", 0.0, 1.0),
            ("minLength", 0.0, 64.0),
        ):
            num_val = raw_node.get(num_key)
            if isinstance(num_val, (int, float)) and not isinstance(num_val, bool) and math.isfinite(float(num_val)):
                clamped = min(max(float(num_val), low), high)
                out[num_key] = int(clamped) if num_key == "maxLines" else clamped

        if isinstance(raw_node.get("tabularNums"), bool):
            out["tabularNums"] = raw_node["tabularNums"]
        if isinstance(raw_node.get("scrim"), bool):
            out["scrim"] = raw_node["scrim"]

        for url_key in ("url", "backgroundUrl"):
            raw_url = raw_node.get(url_key)
            safe_url = cls._sanitize_plugin_asset_path(raw_url)
            if safe_url:
                out[url_key] = safe_url

        raw_action = raw_node.get("action")
        if isinstance(raw_action, Mapping):
            action_kind = str(raw_action.get("type") or "openPlugin").strip()
            if action_kind in {"openPlugin", "pluginAction"}:
                act_obj: dict[str, str] = {"type": action_kind}
                surface = str(raw_action.get("surface") or "data").strip()
                if surface in {"data", "config"}:
                    act_obj["surface"] = surface
                act_id = raw_action.get("actionId")
                if isinstance(act_id, str) and _WIDGET_KEY_RE.fullmatch(act_id.strip()):
                    act_obj["actionId"] = act_id.strip()
                out["action"] = act_obj

        if node_type in {"column", "row", "stack", "box"} and depth < PLUGIN_WIDGET_MAX_DEPTH:
            raw_children = raw_node.get("children")
            if isinstance(raw_children, list):
                children: list[dict[str, Any]] = []
                for child in raw_children[:16]:
                    if budget[0] <= 0:
                        break
                    clean_child = cls._sanitize_layout_node(child, depth=depth + 1, budget=budget)
                    if clean_child is not None:
                        children.append(clean_child)
                out["children"] = children
        return out

    @staticmethod
    def _sanitize_plugin_asset_path(raw_url: Any) -> str | None:
        """
        仅放行同源插件静态资源路径 `/api/v1/plugin/file/...`，拒绝外部任意地址或路径穿越。
        """
        if not isinstance(raw_url, str):
            return None
        candidate = raw_url.strip()
        if (
            not candidate.startswith("/api/v1/plugin/file/")
            or ".." in candidate
            or "\\" in candidate
            or any(ch in candidate for ch in ("\r", "\n", "\x00", " "))
            or len(candidate.encode("utf-8")) > 512
        ):
            return None
        return candidate
