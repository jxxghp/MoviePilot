"""
媒体身份的持久化不变量。

「media_source 与 media_id 必须成对、非零、去空白」这条规则此前由六张表的各个 Oper
在建模前各调一次 normalize_media_identity_payload 来保证——靠调用点的纪律，新加一条
写入路径忘了调，就会静静写进半对身份，而按身份去重从此对这行失效。

这里把它下沉成 flush 前的 mapper 事件：凡同时具备两列的表，任何 ORM 写入都会经过，
忘不掉也绕不开。app/db 里没有 core insert()／bulk 写法（已核对），因此覆盖是完整的。

与 DTO 侧的失败语义有意不同，见下方 _normalize_identity 的说明。
"""
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Mapper

from app.runtime.log import logger
from app.schemas.media import resolve_media_identity

# 构成媒体身份的两列，缺一不可
IDENTITY_COLUMNS = ("media_source", "media_id")


def _normalize_identity(mapper: Mapper, connection: Any, target: Any) -> None:
    """
    写库前归一媒体身份；半对、非法或零值身份清空两列并记一条告警。

    为什么是「清空 + 告警」而不是像 DTO 侧那样抛错：这六张表都是记账性写入（整理历史、
    下载历史、失败冷却、媒体服务器同步、订阅历史）。因身份不成对就让整条记录写不进去，
    等于用一个次要字段的问题换掉整条记账——而丢一条整理历史意味着那个文件可能被重复
    整理。所以持久化侧选择降级保留，但**不再沉默**：告警让半对身份从「查不出的脏数据」
    变成「日志里可检索的事件」。DTO 侧仍然抛错，那里是用户输入的边界，该当场拒绝。

    :param mapper: 触发事件的映射器
    :param connection: 本次 flush 使用的连接，未用到
    :param target: 待写入的模型实例
    """
    columns = mapper.columns.keys()
    if not all(name in columns for name in IDENTITY_COLUMNS):
        return
    raw_source = getattr(target, "media_source", None)
    raw_id = getattr(target, "media_id", None)
    if raw_source is None and raw_id is None:
        return
    media_source, media_id = resolve_media_identity(
        media_source=raw_source, media_id=raw_id
    )
    if not (media_source and media_id):
        # 用映射类名而非 local_table.name：后者的静态类型是 FromClause，没有 name
        logger.warn(
            f"{mapper.class_.__name__} 的媒体身份不成对，已清空："
            f"media_source={raw_source!r}, media_id={raw_id!r}"
        )
    target.media_source = media_source.value if media_source else None
    target.media_id = media_id


def register_identity_normalizer() -> None:
    """
    注册身份归一事件。

    监听 Mapper 类本身而非某个基类：Base 自身没有表、不是映射类，挂不上 mapper 事件；
    挂在 Mapper 上则覆盖进程内全部映射，包括仓外插件自建的模型。开销由上面那行列名
    检查兜住——不具备身份列的表直接返回。
    """
    event.listen(Mapper, "before_insert", _normalize_identity)
    event.listen(Mapper, "before_update", _normalize_identity)


register_identity_normalizer()
