# -*- coding: utf-8 -*-
from types import SimpleNamespace
from unittest.mock import Mock

from app.modules.indexer import IndexerModule
from app.modules.indexer.parser import SiteSchema


def _build_parser_constructor(schema_value: str, calls: list[str]) -> Mock:
    """构造记录解析调用的站点解析器桩。"""
    constructor = Mock(name=f"{schema_value}Parser")
    constructor.schema = SimpleNamespace(value=schema_value)

    def build_parser(**_kwargs):
        """构造一个解析失败且不发起真实请求的解析器实例。"""
        parser = Mock()
        parser.userid = None
        parser.username = None
        parser.user_level = None
        parser.join_at = None
        parser.upload = 0
        parser.download = 0
        parser.ratio = 0
        parser.bonus = 0
        parser.seeding = 0
        parser.seeding_size = 0
        parser.seeding_info = []
        parser.leeching = 0
        parser.leeching_size = 0
        parser.message_unread = 0
        parser.message_unread_contents = []
        parser.err_msg = None
        parser.parse.side_effect = lambda: calls.append(schema_value)
        return parser

    constructor.side_effect = build_parser
    return constructor


def test_refresh_userdata_fallback_only_uses_common_schemas(monkeypatch):
    """声明模型失败时只尝试常用模型，不触发专用模型兜底请求。"""
    calls = []
    parser_constructors = [
        _build_parser_constructor(SiteSchema.NexusPhp.value, calls),
        _build_parser_constructor(SiteSchema.Gazelle.value, calls),
        _build_parser_constructor(SiteSchema.Unit3d.value, calls),
        _build_parser_constructor(SiteSchema.TNode.value, calls),
        _build_parser_constructor(SiteSchema.HDDolby.value, calls),
    ]
    monkeypatch.setattr(IndexerModule, "_site_schemas", parser_constructors)

    module = object.__new__(IndexerModule)
    result = module.refresh_userdata({
        "name": "音乐乌托邦",
        "url": "https://www.musopia.vip/",
        "schema": SiteSchema.NexusPhp.value,
        "public": False,
    })

    assert result.userid is None
    assert calls == [
        SiteSchema.NexusPhp.value,
        SiteSchema.Gazelle.value,
        SiteSchema.Unit3d.value,
    ]
    assert not parser_constructors[3].called
    assert not parser_constructors[4].called
