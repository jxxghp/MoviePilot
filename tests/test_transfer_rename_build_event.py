#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
TransferRenameBuild 事件的单元测试。

通过 patch ``eventmanager.send_event`` 模拟链上插件处理器，避免依赖
MoviePilot 的"插件实例反查"机制（``__get_class_instance`` 要求 handler 位于
``app.<plugin>`` 模块内）。覆盖以下关键路径：

1. 插件就地 mutate ``rename_dict`` 后，主程序首次渲染读取到补充字段；
2. 插件用"返回新 dict"的方式（替换 ``event_data.rename_dict`` 引用）也能生效；
3. 没有任何监听者时（``send_event`` 返回 None），渲染输出与改造前完全一致；
4. ``get_rename_path`` 把正确的 ``source_path`` / ``source_item`` / ``template_string``
   注入到 ``TransferRenameBuildEventData``，便于插件做条件早退。
"""

from unittest.mock import patch

from app.modules.filemanager.transhandler import TransHandler
from app.runtime.events import Event
from app.schemas.event import TransferRenameBuildEventData
from app.schemas.types import ChainEventType

# 只有插件补充 effect 或 codec 时才输出对应片段，用于验证事件修改在首次渲染生效。
TEMPLATE = "{{title}}{% if effect %} {{effect}}{% endif %}{% if codec %} {{codec}}{% endif %}"


def _make_event(event_type: ChainEventType, data) -> Event:
    """
    构造真实 dispatcher 返回的 Event 对象，便于和生产代码读取路径对齐。
    """
    return Event(event_type=event_type.value, event_data=data)


def test_in_place_field_supplement_takes_effect() -> None:
    """
    插件就地补充重命名字段后，首次渲染即使用补充值。
    """
    captured = {}

    def fake_send_event(event_type, data, **_kwargs):
        """
        模拟插件就地补充字段，并记录事件携带的模板与来源路径。
        """
        if event_type is ChainEventType.TransferRenameBuild:
            assert isinstance(data, TransferRenameBuildEventData)
            captured["template_string"] = data.template_string
            captured["source_path"] = data.source_path
            # 模拟插件就地补充字段
            data.rename_dict["effect"] = "SDR"
            return _make_event(event_type, data)
        return _make_event(event_type, data)

    with patch(
        "app.modules.filemanager.transhandler.eventmanager.send_event",
        side_effect=fake_send_event,
    ):
        path = TransHandler.get_rename_path(
            template_string=TEMPLATE,
            rename_dict={"title": "Foo"},
            source_path="/downloads/foo.mkv",
        )

    assert path.as_posix() == "Foo SDR"
    assert captured["template_string"] == TEMPLATE
    assert captured["source_path"] == "/downloads/foo.mkv"


def test_returning_new_dict_reference_is_respected() -> None:
    """
    模拟插件用"完整替换 rename_dict 引用"的写法，验证 get_rename_path 在事件
    返回后会重新取引用，新 dict 中的字段也能被首次渲染读到。
    """

    def fake_send_event(event_type, data, **_kwargs):
        """
        模拟插件替换重命名字典引用后返回事件。
        """
        if event_type is ChainEventType.TransferRenameBuild:
            new_dict = dict(data.rename_dict)
            new_dict["codec"] = "H265"
            data.rename_dict = new_dict
        return _make_event(event_type, data)

    with patch(
        "app.modules.filemanager.transhandler.eventmanager.send_event",
        side_effect=fake_send_event,
    ):
        path = TransHandler.get_rename_path(
            template_string=TEMPLATE,
            rename_dict={"title": "Foo"},
            source_path="/downloads/foo.mkv",
        )

    assert path.as_posix() == "Foo H265"


def test_no_listeners_yields_unchanged_render() -> None:
    """
    监听者缺席时 send_event 返回 None；get_rename_path 应跳过引用刷新并按原
    rename_dict 渲染，行为与改造前完全一致。
    """

    def fake_send_event(event_type, _data, **_kwargs):
        # 真实 dispatcher 在无 enabled handler 时返回 None
        """
        模拟没有监听者时事件分发返回 None。
        """
        return None

    with patch(
        "app.modules.filemanager.transhandler.eventmanager.send_event",
        side_effect=fake_send_event,
    ):
        path = TransHandler.get_rename_path(
            template_string=TEMPLATE,
            rename_dict={"title": "Foo"},
            source_path="/downloads/foo.mkv",
        )

    assert path.as_posix() == "Foo"


def test_event_data_carries_source_metadata() -> None:
    """
    即便没有 source_path（recommend_name 预览场景），事件仍会触发，但
    ``source_path`` / ``source_item`` 都为 None，供插件自行早退。
    """
    captured = {}

    def fake_send_event(event_type, data, **_kwargs):
        """
        记录重命名事件上下文并返回模拟的分发结果。
        """
        if event_type is ChainEventType.TransferRenameBuild:
            captured["source_path"] = data.source_path
            captured["source_item"] = data.source_item
        return _make_event(event_type, data)

    with patch(
        "app.modules.filemanager.transhandler.eventmanager.send_event",
        side_effect=fake_send_event,
    ):
        TransHandler.get_rename_path(
            template_string=TEMPLATE,
            rename_dict={"title": "Foo"},
        )

    assert captured["source_path"] is None
    assert captured["source_item"] is None
