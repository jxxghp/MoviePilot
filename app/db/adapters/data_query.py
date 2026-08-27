"""插件只读数据查询的 SQLAlchemy 持久化适配器。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.application.data_query import QueryRows
from app.db.base import Base
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory as SubscribeHistoryModel
from app.db.models.transferhistory import TransferHistory
from app.schemas.history import (
    DownloadHistory as DownloadHistoryView,
)
from app.schemas.history import (
    TransferHistory as TransferHistoryView,
)
from app.schemas.query import (
    DownloadHistoryFilter,
    QueryPageRequest,
    QuerySortDirection,
    QuerySortField,
    SubscriptionFilter,
    SubscriptionHistoryFilter,
    TransferHistoryFilter,
)
from app.schemas.query import SubscribeHistory as SubscribeHistoryView
from app.schemas.subscribe import Subscribe as SubscribeView
from app.schemas.types import MUSIC_ENTITY_RECORDING

_ModelT = TypeVar("_ModelT", bound=Base)
_ViewT = TypeVar("_ViewT", bound=BaseModel)


def _enum_value(value: Any) -> Any:
    """返回枚举筛选值的稳定数据库表示。"""
    return value.value if isinstance(value, Enum) else value


def _values(values: Iterable[Any]) -> tuple[Any, ...]:
    """去除空筛选值并保留调用方声明的顺序。"""
    normalized: list[Any] = []
    for value in values:
        value = _enum_value(value)
        if value in (None, ""):
            continue
        normalized.append(value)
    return tuple(dict.fromkeys(normalized))


def _contains(column: Any, value: str) -> Any:
    """构造不区分大小写且不解释通配符的字面包含筛选。"""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.ilike(f"%{escaped}%", escape="\\")


def _music_type_condition(column: Any, music_type: str | None) -> Any | None:
    """兼容未标注音乐类型的历史单曲记录。"""
    if not music_type:
        return None
    if music_type == MUSIC_ENTITY_RECORDING:
        return or_(column == music_type, column.is_(None))
    return column == music_type


class SqlAlchemyDataQueryAdapter:
    """以短生命周期同步 Session 执行统一只读分页查询。

    这个适配器是查询层唯一接触 SQLAlchemy Model 的边界。每个公开方法在同一
    Session 中先统计再读取当前页，并在 Session 仍有效时转换成 Pydantic DTO，
    因而调用方不会持有 ORM 实例或延迟加载状态。
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """保存由启动组合根提供的同步 Session 工厂。"""
        self._session_factory = session_factory

    @staticmethod
    def _identity_conditions(model: Any, query: Any) -> list[Any]:
        """按媒体来源与原生 ID 的成对合同构造筛选条件。"""
        media_source = query.media_source
        media_id = query.media_id
        if (media_source is None) != (media_id is None):
            # Pydantic 合同会先拒绝这种输入；这里仍保留拒绝，避免未校验对象
            # 在持久化边界退化成只按 NULL 查询而扩大结果集。
            raise ValueError("media_source 和 media_id 必须同时提供")
        if media_source is None:
            return []
        normalized_id = str(media_id).strip()
        if not normalized_id or normalized_id == "0":
            raise ValueError("media_id 必须是非零的来源原生 ID")
        return [
            model.media_source == _enum_value(media_source),
            model.media_id == normalized_id,
        ]

    @staticmethod
    def _require_media_identity(model: Any) -> list[Any]:
        """只保留来源和原生 ID 均存在且非空的记录。"""
        return [
            model.media_source.is_not(None),
            func.trim(model.media_source) != "",
            model.media_id.is_not(None),
            func.trim(model.media_id) != "",
            func.trim(model.media_id) != "0",
        ]

    @staticmethod
    def _order_by(model: Any, request: QueryPageRequest) -> tuple[Any, ...]:
        """构造可跨页复现的排序，日期相同时始终以主键打破平局。"""
        descending = request.sort.direction == QuerySortDirection.DESC
        if request.sort.field == QuerySortField.ID:
            primary = model.id.desc() if descending else model.id.asc()
            secondary = model.date.desc() if descending else model.date.asc()
            return primary, secondary
        primary = model.date.desc().nullslast() if descending else model.date.asc().nullsfirst()
        secondary = model.id.desc() if descending else model.id.asc()
        return primary, secondary

    def _page(
        self,
        *,
        model: type[_ModelT],
        view_model: type[_ViewT],
        conditions: Iterable[Any],
        page: QueryPageRequest,
    ) -> QueryRows[_ViewT]:
        """在单个 Session 内完成 count、分页读取和 DTO 投影。"""
        conditions = tuple(conditions)
        count_statement = select(func.count(model.id))
        page_statement = select(model)
        if conditions:
            count_statement = count_statement.where(*conditions)
            page_statement = page_statement.where(*conditions)
        page_statement = (
            page_statement.order_by(*self._order_by(model, page)).offset((page.page - 1) * page.count).limit(page.count)
        )

        with self._session_factory() as session:
            total = int(session.execute(count_statement).scalar_one() or 0)
            records = session.execute(page_statement).scalars().all()
            # model_validate 必须在会话内完成；返回值只包含 Pydantic 数据。
            items = [view_model.model_validate(record) for record in records]
            return QueryRows(items=items, total=total)

    def _get(
        self,
        *,
        model: type[_ModelT],
        view_model: type[_ViewT],
        record_id: int,
    ) -> _ViewT | None:
        """在短 Session 内按主键读取并冻结单条 Pydantic 投影。"""
        statement = select(model).where(model.id == record_id)
        with self._session_factory() as session:
            record = session.execute(statement).scalars().first()
            return view_model.model_validate(record) if record is not None else None

    def list_subscriptions(
        self,
        *,
        filters: SubscriptionFilter,
        page: QueryPageRequest,
    ) -> QueryRows[SubscribeView]:
        """按受控组合条件分页查询当前订阅。"""
        query = filters
        conditions = self._identity_conditions(Subscribe, query)
        ids = _values(query.ids)
        names = _values(query.names)
        states = _values(query.states)
        usernames = _values(query.usernames)
        media_types = _values(query.media_types)
        if ids:
            conditions.append(Subscribe.id.in_(ids))
        if names:
            conditions.append(Subscribe.name.in_(names))
        if states:
            conditions.append(Subscribe.state.in_(states))
        if usernames:
            conditions.append(Subscribe.username.in_(usernames))
        if media_types:
            conditions.append(Subscribe.type.in_(media_types))
        if query.season is not None:
            conditions.append(Subscribe.season == query.season)
        if query.episode_group is not None:
            conditions.append(Subscribe.episode_group == query.episode_group)
        music_condition = _music_type_condition(Subscribe.music_type, query.music_type)
        if music_condition is not None:
            conditions.append(music_condition)
        return self._page(
            model=Subscribe,
            view_model=SubscribeView,
            conditions=conditions,
            page=page,
        )

    def get_subscription(self, subscription_id: int) -> SubscribeView | None:
        """按主键查询订阅并返回脱离 Session 的 DTO。"""
        return self._get(
            model=Subscribe,
            view_model=SubscribeView,
            record_id=subscription_id,
        )

    def list_subscription_history(
        self,
        *,
        filters: SubscriptionHistoryFilter,
        page: QueryPageRequest,
    ) -> QueryRows[SubscribeHistoryView]:
        """按受控组合条件分页查询订阅完成历史。"""
        query = filters
        conditions = self._identity_conditions(SubscribeHistoryModel, query)
        ids = _values(query.ids)
        names = _values(query.names)
        usernames = _values(query.usernames)
        media_types = _values(query.media_types)
        if ids:
            conditions.append(SubscribeHistoryModel.id.in_(ids))
        if names:
            conditions.append(SubscribeHistoryModel.name.in_(names))
        if usernames:
            conditions.append(SubscribeHistoryModel.username.in_(usernames))
        if media_types:
            conditions.append(SubscribeHistoryModel.type.in_(media_types))
        if query.season is not None:
            conditions.append(SubscribeHistoryModel.season == query.season)
        if query.episode_group is not None:
            conditions.append(SubscribeHistoryModel.episode_group == query.episode_group)
        music_condition = _music_type_condition(
            SubscribeHistoryModel.music_type,
            query.music_type,
        )
        if music_condition is not None:
            conditions.append(music_condition)
        return self._page(
            model=SubscribeHistoryModel,
            view_model=SubscribeHistoryView,
            conditions=conditions,
            page=page,
        )

    def get_subscription_history(
        self,
        history_id: int,
    ) -> SubscribeHistoryView | None:
        """按主键查询订阅完成历史并返回稳定 DTO。"""
        return self._get(
            model=SubscribeHistoryModel,
            view_model=SubscribeHistoryView,
            record_id=history_id,
        )

    def list_download_history(
        self,
        *,
        filters: DownloadHistoryFilter,
        page: QueryPageRequest,
    ) -> QueryRows[DownloadHistoryView]:
        """按受控组合条件分页查询下载历史。"""
        query = filters
        conditions = self._identity_conditions(DownloadHistory, query)
        ids = _values(query.ids)
        media_types = _values(query.media_types)
        usernames = _values(query.usernames)
        if ids:
            conditions.append(DownloadHistory.id.in_(ids))
        if media_types:
            conditions.append(DownloadHistory.type.in_(media_types))
        for column, value in (
            (DownloadHistory.title, query.title),
            (DownloadHistory.path, query.path),
        ):
            if value:
                conditions.append(_contains(column, value))
        for column, value in (
            (DownloadHistory.year, query.year),
            (DownloadHistory.seasons, query.seasons),
            (DownloadHistory.episodes, query.episodes),
            (DownloadHistory.download_hash, query.download_hash),
            (DownloadHistory.username, query.username),
            (DownloadHistory.episode_group, query.episode_group),
        ):
            if value is not None and value != "":
                conditions.append(column == value)
        if usernames:
            conditions.append(DownloadHistory.username.in_(usernames))
        music_condition = _music_type_condition(DownloadHistory.music_type, query.music_type)
        if music_condition is not None:
            conditions.append(music_condition)
        return self._page(
            model=DownloadHistory,
            view_model=DownloadHistoryView,
            conditions=conditions,
            page=page,
        )

    def get_download_history(self, history_id: int) -> DownloadHistoryView | None:
        """按主键查询下载历史并返回稳定 DTO。"""
        return self._get(
            model=DownloadHistory,
            view_model=DownloadHistoryView,
            record_id=history_id,
        )

    def list_transfer_history(
        self,
        *,
        filters: TransferHistoryFilter,
        page: QueryPageRequest,
    ) -> QueryRows[TransferHistoryView]:
        """按受控组合条件分页查询整理历史。"""
        query = filters
        conditions = self._identity_conditions(TransferHistory, query)
        ids = _values(query.ids)
        media_types = _values(query.media_types)
        media_sources = _values(query.media_sources)
        if ids:
            conditions.append(TransferHistory.id.in_(ids))
        if media_types:
            conditions.append(TransferHistory.type.in_(media_types))
        if media_sources:
            conditions.append(TransferHistory.media_source.in_(media_sources))
        if query.require_media_identity:
            conditions.extend(self._require_media_identity(TransferHistory))
        if query.title:
            conditions.append(_contains(TransferHistory.title, query.title))
        if query.text:
            conditions.append(
                or_(
                    _contains(TransferHistory.title, query.text),
                    _contains(TransferHistory.src, query.text),
                    _contains(TransferHistory.dest, query.text),
                )
            )
        for column, value in (
            (TransferHistory.year, query.year),
            (TransferHistory.seasons, query.seasons),
            (TransferHistory.episodes, query.episodes),
            (TransferHistory.src, query.src),
            (TransferHistory.dest, query.dest),
            (TransferHistory.download_hash, query.download_hash),
            (TransferHistory.episode_group, query.episode_group),
        ):
            if value is not None and value != "":
                conditions.append(column == value)
        if query.status is not None:
            conditions.append(TransferHistory.status == query.status)
        music_condition = _music_type_condition(TransferHistory.music_type, query.music_type)
        if music_condition is not None:
            conditions.append(music_condition)
        return self._page(
            model=TransferHistory,
            view_model=TransferHistoryView,
            conditions=conditions,
            page=page,
        )

    def get_transfer_history(self, history_id: int) -> TransferHistoryView | None:
        """按主键查询整理历史并返回稳定 DTO。"""
        return self._get(
            model=TransferHistory,
            view_model=TransferHistoryView,
            record_id=history_id,
        )


__all__ = ["SqlAlchemyDataQueryAdapter"]
