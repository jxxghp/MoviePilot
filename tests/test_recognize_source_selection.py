# -*- coding: utf-8 -*-
"""识别测试类请求的数据源选择回归测试。

识别测试（名称测试）前端只传请求级数据源 media_source，不携带 media_id；
该路径必须按名称在指定数据源内识别，而不是被"显式身份必须成对"规则拦截。
"""
import asyncio
from unittest.mock import AsyncMock, Mock, patch

from app.application.orchestration import ChainBase
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaSource, MediaType

# 用户反馈的识别测试失败样例
FAILING_TITLE = "[ANI] 關於我轉生變成史萊姆這檔事 第四季 - 90 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"


def _tmdb_media() -> MediaInfo:
    """构造带 TMDB 身份的识别结果。"""
    return MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="120089",
        tmdb_id=120089,
        title="关于我转生变成史莱姆这档事",
        type=MediaType.TV,
    )


def test_recognize_media_with_source_only_uses_name_search():
    """仅传 media_source 时应按名称在指定数据源识别，不得直接拒绝。"""
    chain = ChainBase()
    meta = MetaInfo(FAILING_TITLE)
    captured = {}

    def fake_native(module_kwargs, cache):
        captured.update(module_kwargs)
        return _tmdb_media()

    with patch.object(chain, "_run_native_media_recognize", side_effect=fake_native), \
            patch.object(chain, "_supplement_media_recognize", side_effect=lambda **kw: kw["mediainfo"]), \
            patch("app.application.orchestration._recognition.MoviePilotServerHelper"):
        result = chain.recognize_media(meta=meta, media_source=MediaSource.TMDB, cache=False)

    assert result is not None
    assert result.tmdb_id == 120089
    # 数据源约束透传到模块层，且无显式 ID
    assert captured["media_source"] == MediaSource.TMDB
    assert captured["media_id"] is None
    # 名称识别应沿用 meta 推断的类型
    assert captured["mtype"] == MediaType.TV


def test_async_recognize_media_with_source_only_uses_name_search():
    """异步入口与同步入口保持同一语义。"""
    chain = ChainBase()
    meta = MetaInfo(FAILING_TITLE)
    captured = {}

    async def fake_native(module_kwargs, cache):
        captured.update(module_kwargs)
        return _tmdb_media()

    async def fake_supplement(**kwargs):
        return kwargs["mediainfo"]

    # 异步路径上报共享识别为协程，需要可 await 的桩
    helper = Mock()
    helper.async_report_recognize_share = AsyncMock()

    with patch.object(chain, "_async_run_native_media_recognize", side_effect=fake_native), \
            patch.object(chain, "_async_supplement_media_recognize", side_effect=fake_supplement), \
            patch("app.application.orchestration._recognition.MoviePilotServerHelper", helper):
        result = asyncio.run(
            chain.async_recognize_media(meta=meta, media_source=MediaSource.TMDB, cache=False)
        )

    assert result is not None
    assert result.tmdb_id == 120089
    assert captured["media_source"] == MediaSource.TMDB
    assert captured["media_id"] is None


def test_recognize_media_accepts_string_source_only():
    """第三方客户端可能传字符串数据源，应规范为枚举后按名称识别。"""
    chain = ChainBase()
    meta = MetaInfo(FAILING_TITLE)
    captured = {}

    def fake_native(module_kwargs, cache):
        captured.update(module_kwargs)
        return _tmdb_media()

    with patch.object(chain, "_run_native_media_recognize", side_effect=fake_native), \
            patch.object(chain, "_supplement_media_recognize", side_effect=lambda **kw: kw["mediainfo"]), \
            patch("app.application.orchestration._recognition.MoviePilotServerHelper"):
        result = chain.recognize_media(meta=meta, media_source="themoviedb", cache=False)

    assert result is not None
    assert captured["media_source"] == MediaSource.TMDB


def test_recognize_media_meta_identity_same_source_uses_id():
    """仅传数据源且 meta 自带同源身份（如 {tmdbid=}）时应直接按身份识别。"""
    chain = ChainBase()
    meta = MetaInfo("空之境界 第五章 矛盾螺旋 (2008) {tmdbid=23155}")
    captured = {}

    def fake_native(module_kwargs, cache):
        captured.update(module_kwargs)
        return _tmdb_media()

    with patch.object(chain, "_run_native_media_recognize", side_effect=fake_native), \
            patch.object(chain, "_supplement_media_recognize", side_effect=lambda **kw: kw["mediainfo"]), \
            patch("app.application.orchestration._recognition.MoviePilotServerHelper"):
        result = chain.recognize_media(meta=meta, media_source=MediaSource.TMDB, cache=False)

    assert result is not None
    assert captured["media_source"] == MediaSource.TMDB
    assert captured["media_id"] == "23155"


def test_recognize_media_media_id_without_source_still_rejected():
    """显式 media_id 缺少有效来源时仍应拒绝，成对约束不放宽。"""
    chain = ChainBase()
    native = Mock()

    with patch.object(chain, "_run_native_media_recognize", native):
        result = chain.recognize_media(meta=MetaInfo("任意标题"), media_id="12345")

    assert result is None
    native.assert_not_called()


def test_recognize_media_invalid_pair_still_rejected():
    """media_id 为 0 等无效值与来源组合仍应拒绝。"""
    chain = ChainBase()
    native = Mock()

    with patch.object(chain, "_run_native_media_recognize", native):
        result = chain.recognize_media(
            meta=MetaInfo("任意标题"), media_source=MediaSource.TMDB, media_id="0"
        )

    assert result is None
    native.assert_not_called()
