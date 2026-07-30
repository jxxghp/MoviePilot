import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.chain import ChainBase
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.helper.server import MoviePilotServerHelper
from app.schemas.types import MediaType, SystemConfigKey


def _build_meta(name: str) -> MetaBase:
    """构造共享识别统计测试所需的媒体元数据。"""
    meta = MetaBase(name)
    meta.name = name
    meta.type = MediaType.UNKNOWN
    return meta


def _shared_params(tmdb_id: int) -> dict:
    """构造共享识别结果转换后的模块参数。"""
    return {
        "mtype": MediaType.MOVIE,
        "source": "themoviedb",
        "mediaid": str(tmdb_id),
        "tmdbid": tmdb_id,
        "doubanid": None,
        "bangumiid": None,
        "anilistid": None,
    }


def _mock_counter(monkeypatch) -> Mock:
    """替换系统配置持久化入口并返回递增调用桩。"""
    increment = Mock()
    monkeypatch.setattr(
        "app.chain.SystemConfigOper",
        lambda: SimpleNamespace(increment=increment),
    )
    return increment


def test_sync_shared_recognize_success_increments_persisted_count(monkeypatch):
    """同步共享识别二次识别成功后应累计一次命中。"""
    chain = object.__new__(ChainBase)
    meta = _build_meta("共享识别电影")
    media = MediaInfo(
        title="共享识别电影",
        year="2026",
        tmdb_id=101,
        type=MediaType.MOVIE,
    )
    increment = _mock_counter(monkeypatch)
    monkeypatch.setattr("app.chain.settings.MEDIA_RECOGNIZE_SHARE", True)
    monkeypatch.setattr(chain, "run_module", Mock(side_effect=[None, media]))
    monkeypatch.setattr(chain, "_update_local_recognize_cache", Mock())
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "query_recognize_share",
        Mock(return_value={"type": "movie", "tmdbid": 101}),
    )
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "to_recognize_params",
        Mock(return_value=_shared_params(101)),
    )

    result = chain.recognize_media(meta=meta, cache=False)

    assert result is media
    increment.assert_called_once_with(SystemConfigKey.MediaRecognizeShareCount)


def test_sync_shared_result_without_local_match_does_not_increment(monkeypatch):
    """共享接口返回数据但二次识别失败时不应累计命中。"""
    chain = object.__new__(ChainBase)
    meta = _build_meta("共享识别失败电影")
    increment = _mock_counter(monkeypatch)
    monkeypatch.setattr("app.chain.settings.MEDIA_RECOGNIZE_SHARE", True)
    monkeypatch.setattr(chain, "run_module", Mock(side_effect=[None, None]))
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "query_recognize_share",
        Mock(return_value={"type": "movie", "tmdbid": 102}),
    )
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "to_recognize_params",
        Mock(return_value=_shared_params(102)),
    )

    result = chain.recognize_media(meta=meta, cache=False)

    assert result is None
    increment.assert_not_called()


def test_async_shared_recognize_success_increments_persisted_count(monkeypatch):
    """异步共享识别二次识别成功后应累计一次命中。"""
    chain = object.__new__(ChainBase)
    meta = _build_meta("异步共享识别电影")
    media = MediaInfo(
        title="异步共享识别电影",
        year="2026",
        tmdb_id=103,
        type=MediaType.MOVIE,
    )
    increment = _mock_counter(monkeypatch)
    monkeypatch.setattr("app.chain.settings.MEDIA_RECOGNIZE_SHARE", True)
    monkeypatch.setattr(
        chain,
        "async_run_module",
        AsyncMock(side_effect=[None, media]),
    )
    monkeypatch.setattr(
        chain,
        "_async_update_local_recognize_cache",
        AsyncMock(),
    )
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "async_query_recognize_share",
        AsyncMock(return_value={"type": "movie", "tmdbid": 103}),
    )
    monkeypatch.setattr(
        MoviePilotServerHelper,
        "to_recognize_params",
        Mock(return_value=_shared_params(103)),
    )

    result = asyncio.run(chain.async_recognize_media(meta=meta, cache=False))

    assert result is media
    increment.assert_called_once_with(SystemConfigKey.MediaRecognizeShareCount)
