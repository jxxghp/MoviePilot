"""
订阅写入应用用例。

本模块把 MediaInfo / MusicInfo 翻译成一行订阅，是订阅表的唯一业务写入口。翻译此前
长在 SubscribeOper.add 上，但取标题、选海报尺寸、判音乐实体、决定哪几个字段构成一条
订阅的身份，都是订阅业务的规则而非数据访问——Oper 只该收敛查询，领域对象不该出现在
它的入参里。搬上来之后 SubscribeOper 收到的是纯粹的持久化字典，与
app/application/history.py 里整理历史的写入路径同构。

留在 Oper 的是列类型强转与建库时间戳：那几步是为 PostgreSQL 的严格类型检查和订阅表
自己的列类型而存在的，跟着列走比跟着调用方走更不容易漂。

字段映射错了不会报错，只会让订阅静静地记错——而搜索、洗版、完成判定、去重全都读这
张表。同步与异步是两份逐字复制的实现，改一条漏一条就是真实缺陷，故翻译与身份构造由
下方 _translate 单点承担，两条链路只在「怎么查、怎么写」上分叉。
"""

from collections.abc import Awaitable, Callable
from typing import Optional, Protocol, Tuple

from app.domain.context import MediaInfo, MusicInfo
from app.schemas.media import resolve_media_identity
from app.schemas.types import MUSIC_ENTITY_ALBUM, MediaType

# 身份不完整时的固定返回。身份不全的订阅写进去就是一条永远匹配不上资源的僵尸订阅，
# 而后续按身份去重也会失效，所以必须在查询与建模之前短路
INCOMPLETE_IDENTITY = (0, "媒体身份不完整")

AfterCommitEffect = Callable[[int], None]
AsyncAfterCommitEffect = Callable[[int], Awaitable[None]]


class SubscribeWriter(Protocol):
    """订阅写入应用服务使用的数据端口。"""

    def add(
        self,
        identity: dict,
        payload: dict,
        username: Optional[str] = None,
        after_commit: Optional[AfterCommitEffect] = None,
    ) -> Tuple[int, str]:
        """同步新增订阅，并在事务成功后执行外部副作用。"""

    async def async_add(
        self,
        identity: dict,
        payload: dict,
        username: Optional[str] = None,
        after_commit: Optional[AsyncAfterCommitEffect] = None,
    ) -> Tuple[int, str]:
        """异步新增订阅，并在事务成功后执行外部副作用。"""


class StagedSubscription(Protocol):
    """订阅仓储暂存结果的结构化端口，避免 Application 反向约束适配器类型。"""

    @property
    def subscribe_id(self) -> int:
        """返回已创建或已存在的订阅 ID。"""
        ...

    @property
    def message(self) -> str:
        """返回兼容旧入口的结果说明。"""
        ...

    @property
    def created(self) -> bool:
        """标识本次是否暂存了一条新记录。"""
        ...


class SubscriptionStagingRepository(Protocol):
    """新增订阅命令需要的无提交仓储端口。"""

    def stage_add(
        self,
        identity: dict,
        payload: dict,
        username: Optional[str] = None,
    ) -> StagedSubscription:
        """暂存同步新增，命中重复订阅时不写入。"""
        ...

    async def async_stage_add(
        self,
        identity: dict,
        payload: dict,
        username: Optional[str] = None,
    ) -> StagedSubscription:
        """暂存异步新增，命中重复订阅时不写入。"""
        ...


class UnitOfWork(Protocol):
    """同步订阅新增命令使用的最小事务端口。"""

    def commit(self) -> None:
        """提交当前逻辑操作。"""
        ...

    def rollback(self) -> None:
        """回滚当前逻辑操作。"""
        ...


class AsyncUnitOfWork(Protocol):
    """异步订阅新增命令使用的最小事务端口。"""

    async def commit(self) -> None:
        """提交当前逻辑操作。"""
        ...

    async def rollback(self) -> None:
        """回滚当前逻辑操作。"""
        ...


class CreateSubscriptionCommand:
    """暂存并提交一条同步订阅，重复请求保持历史返回且不产生提交。"""

    def __init__(
        self,
        repository: SubscriptionStagingRepository,
        unit_of_work: UnitOfWork,
    ) -> None:
        """注入无提交仓储和事务所有者。"""
        self._repository = repository
        self._unit_of_work = unit_of_work

    def execute(
        self,
        identity: dict,
        payload: dict,
        username: Optional[str] = None,
        after_commit: Optional[AfterCommitEffect] = None,
    ) -> Tuple[int, str]:
        """执行同步新增；事务失败回滚，提交后副作用失败不反向回滚。"""
        try:
            staged = self._repository.stage_add(identity, payload, username)
            if staged.created:
                self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise
        if staged.subscribe_id and after_commit:
            after_commit(staged.subscribe_id)
        return staged.subscribe_id, staged.message


class AsyncCreateSubscriptionCommand:
    """暂存并提交一条异步订阅，事务成功后才把结果交给副作用调用方。"""

    def __init__(
        self,
        repository: SubscriptionStagingRepository,
        unit_of_work: AsyncUnitOfWork,
    ) -> None:
        """注入无提交异步仓储和事务所有者。"""
        self._repository = repository
        self._unit_of_work = unit_of_work

    async def execute(
        self,
        identity: dict,
        payload: dict,
        username: Optional[str] = None,
        after_commit: Optional[AsyncAfterCommitEffect] = None,
    ) -> Tuple[int, str]:
        """执行异步新增；事务失败回滚，提交后副作用失败不反向回滚。"""
        try:
            staged = await self._repository.async_stage_add(
                identity,
                payload,
                username,
            )
            if staged.created:
                await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise
        if staged.subscribe_id and after_commit:
            await after_commit(staged.subscribe_id)
        return staged.subscribe_id, staged.message


_configured_subscribe_writer: Callable[[], SubscribeWriter] | None = None


def configure_subscribe_writer(provider: Callable[[], SubscribeWriter]) -> None:
    """由启动组合根登记订阅写入端口提供器。"""
    global _configured_subscribe_writer
    _configured_subscribe_writer = provider


def _get_subscribe_writer(writer: Optional[SubscribeWriter]) -> SubscribeWriter:
    """获取显式传入或启动组合根登记的订阅写入端口。"""
    if writer is not None:
        return writer
    if _configured_subscribe_writer is None:
        raise RuntimeError("订阅写入端口尚未配置")
    return _configured_subscribe_writer()


def _music_entity(mediainfo: MediaInfo | MusicInfo) -> Optional[str]:
    """
    取音乐实体类型；非音乐媒体一律为空。

    影视订阅带着 music_type 会被音乐去重逻辑当成音乐实体，造成串号。查重身份与写入
    字段都取这一个值，避免两处各算一遍后悄悄分叉。
    :param mediainfo: 识别结果
    :return: 音乐实体类型，非音乐媒体为 None
    """
    if mediainfo.type != MediaType.MUSIC:
        return None
    return getattr(mediainfo, "music_type", None)


def _translate(
    mediainfo: MediaInfo | MusicInfo,
    kwargs: dict,
) -> Optional[Tuple[dict, dict, Optional[str]]]:
    """
    把识别结果翻译成查重身份与写入字段。

    :param mediainfo: 识别结果
    :param kwargs: 调用方传入的订阅设置，媒体相关的同名字段会被识别结果覆盖
    :return: (查重身份, 写入字段, 限定用户)；媒体身份不完整时返回 None
    """
    owner_scope = bool(kwargs.pop("owner_scope", False))
    username = kwargs.get("username") if owner_scope else None
    media_source, media_id = resolve_media_identity(
        media=mediainfo,
        media_source=kwargs.get("media_source"),
        media_id=kwargs.get("media_id"),
    )
    if not media_source or not media_id:
        return None
    music_type = _music_entity(mediainfo)
    identity = {
        "media_source": str(media_source),
        "media_id": media_id,
        "music_type": music_type,
        "season": kwargs.get("season"),
        "episode_group": mediainfo.episode_group,
    }
    payload = dict(kwargs)
    payload.update({
        "name": mediainfo.title,
        "year": mediainfo.year,
        "type": mediainfo.type.value,
        "media_source": str(media_source),
        "media_id": media_id,
        "episode_group": mediainfo.episode_group,
        "poster": mediainfo.get_poster_image(),
        "backdrop": mediainfo.get_backdrop_image(),
        "vote": mediainfo.vote_average,
        "description": mediainfo.overview,
        "music_type": music_type,
        # 整专完成判定拿 total_tracks 当分母，单曲带着专辑的曲目数会永远判不到完成
        "total_tracks": getattr(mediainfo, "total_tracks", None)
        if music_type == MUSIC_ENTITY_ALBUM else None,
    })
    return identity, payload, username


def add_subscribe(
    mediainfo: MediaInfo | MusicInfo,
    subscribe_oper: Optional[SubscribeWriter] = None,
    after_commit: Optional[AfterCommitEffect] = None,
    **kwargs,
) -> Tuple[int, str]:
    """
    新增订阅。

    :param mediainfo: 识别结果
    :param subscribe_oper: 复用的订阅操作对象，未传时由启动组合根提供
    :param after_commit: 数据提交后执行的消息、事件或上报编排
    :param kwargs: 订阅设置；owner_scope 为真时按用户名限定查重范围
    :return: (订阅 ID, 结果说明)；ID 为 0 表示未新增
    """
    translated = _translate(mediainfo, kwargs)
    if translated is None:
        return INCOMPLETE_IDENTITY
    identity, payload, username = translated
    oper = _get_subscribe_writer(subscribe_oper)
    if after_commit is None:
        return oper.add(identity=identity, payload=payload, username=username)
    return oper.add(
        identity=identity,
        payload=payload,
        username=username,
        after_commit=after_commit,
    )


async def async_add_subscribe(
    mediainfo: MediaInfo | MusicInfo,
    subscribe_oper: Optional[SubscribeWriter] = None,
    after_commit: Optional[AsyncAfterCommitEffect] = None,
    **kwargs,
) -> Tuple[int, str]:
    """
    异步新增订阅。

    :param mediainfo: 识别结果
    :param subscribe_oper: 复用的订阅操作对象，未传时由启动组合根提供
    :param after_commit: 数据提交后执行的异步消息、事件或上报编排
    :param kwargs: 订阅设置；owner_scope 为真时按用户名限定查重范围
    :return: (订阅 ID, 结果说明)；ID 为 0 表示未新增
    """
    translated = _translate(mediainfo, kwargs)
    if translated is None:
        return INCOMPLETE_IDENTITY
    identity, payload, username = translated
    oper = _get_subscribe_writer(subscribe_oper)
    if after_commit is None:
        return await oper.async_add(identity=identity, payload=payload, username=username)
    return await oper.async_add(
        identity=identity,
        payload=payload,
        username=username,
        after_commit=after_commit,
    )


__all__ = [
    "AfterCommitEffect",
    "AsyncCreateSubscriptionCommand",
    "AsyncAfterCommitEffect",
    "AsyncUnitOfWork",
    "CreateSubscriptionCommand",
    "INCOMPLETE_IDENTITY",
    "StagedSubscription",
    "SubscriptionStagingRepository",
    "SubscribeWriter",
    "UnitOfWork",
    "add_subscribe",
    "async_add_subscribe",
    "configure_subscribe_writer",
]
