"""验证 API 将 Agent 英文类型与原生中文类型传入相同业务分支。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.api.endpoints import douban, media, mediaserver, subscribe, tmdb, transfer
from app.schemas.file import FileItem
from app.schemas.subscribe import Subscribe
from app.schemas.transfer import ManualTransferItem
from app.schemas.types import MediaSource, MediaType
from app.schemas.workflow import MediaInfo

MEDIA_TYPES = [
    ("movie", MediaType.MOVIE),
    ("tv", MediaType.TV),
    ("music", MediaType.MUSIC),
    ("电影", MediaType.MOVIE),
    ("电视剧", MediaType.TV),
    ("音乐", MediaType.MUSIC),
    (" MOVIE ", MediaType.MOVIE),
    (" TV ", MediaType.TV),
    (" MUSIC ", MediaType.MUSIC),
]
METADATA_ENDPOINTS = [
    (douban, "DoubanChain", "douban_credits", "credits", "doubanid"),
    (douban, "DoubanChain", "douban_recommend", "recommend", "doubanid"),
    (tmdb, "TmdbChain", "tmdb_similar", "similar", "tmdbid"),
    (tmdb, "TmdbChain", "tmdb_recommend", "recommend", "tmdbid"),
    (tmdb, "TmdbChain", "tmdb_credits", "credits", "tmdbid"),
]


@pytest.mark.parametrize("type_name,expected", MEDIA_TYPES)
@pytest.mark.parametrize("module,chain_name,endpoint,operation,id_key", METADATA_ENDPOINTS)
def test_metadata_endpoints_route_media_types(
    monkeypatch, type_name, expected, module, chain_name, endpoint, operation, id_key,
) -> None:
    """影视英文和中文类型选择相同方法，音乐类型保持空列表。"""
    movie_call = AsyncMock(return_value=[])
    tv_call = AsyncMock(return_value=[])
    chain = SimpleNamespace(**{
        f"async_movie_{operation}": movie_call,
        f"async_tv_{operation}": tv_call,
    })
    monkeypatch.setattr(module, chain_name, lambda: chain)

    result = asyncio.run(getattr(module, endpoint)(
        **{id_key: "123" if id_key == "doubanid" else 123},
        type_name=type_name,
    ))

    assert result == []
    assert movie_call.await_count == int(expected == MediaType.MOVIE)
    assert tv_call.await_count == int(expected == MediaType.TV)


@pytest.mark.parametrize("type_name,expected", MEDIA_TYPES)
def test_media_detail_forwards_normalized_type(monkeypatch, type_name, expected) -> None:
    """详情入口应将三类中英文类型都转换成枚举再交给识别链。"""
    recognize = AsyncMock(return_value=None)
    monkeypatch.setattr(media, "MediaChain", lambda: SimpleNamespace(
        async_recognize_media=recognize,
    ))

    asyncio.run(media.detail(
        media_id="123", media_source=MediaSource.TMDB, type_name=type_name,
    ))

    recognize.assert_awaited_once_with(
        media_source=MediaSource.TMDB, media_id="123", mtype=expected,
    )


@pytest.mark.parametrize("type_name,expected", MEDIA_TYPES)
def test_create_subscription_forwards_normalized_type(monkeypatch, type_name, expected) -> None:
    """新增订阅应兼容 Agent 类型，并向订阅链传入准确枚举。"""
    add = AsyncMock(return_value=(1, ""))
    monkeypatch.setattr(subscribe, "SubscribeChain", lambda: SimpleNamespace(async_add=add))

    result = asyncio.run(subscribe.create_subscribe(
        subscribe_in=Subscribe(name="测试媒体", type=type_name),
        current_user=SimpleNamespace(name="admin", is_superuser=True),
    ))

    assert result.success is True
    assert add.await_args.kwargs["mtype"] is expected


@pytest.mark.parametrize("type_name,expected", MEDIA_TYPES)
def test_popular_subscriptions_normalize_record_type(monkeypatch, type_name, expected) -> None:
    """共享订阅记录使用英文类型时，公开响应仍输出原生中文类型。"""
    statistic = AsyncMock(return_value=[{
        "name": "测试媒体", "type": type_name, "count": 1,
    }])
    monkeypatch.setattr(
        subscribe.MoviePilotServerHelper, "async_get_subscribe_statistic", statistic,
    )

    result = asyncio.run(subscribe.popular_subscribes(stype="all"))

    assert len(result) == 1
    assert result[0]["type"] == expected.value


@pytest.mark.parametrize("type_name,expected", MEDIA_TYPES + [(None, MediaType.UNKNOWN)])
def test_library_missing_forwards_meta_type(monkeypatch, type_name, expected) -> None:
    """缺失查询应向下载链传入解析后的类型，空值沿用标题推断。"""
    missing = Mock(return_value=(True, {}))
    monkeypatch.setattr(mediaserver, "DownloadChain", lambda: SimpleNamespace(
        get_no_exists_info=missing,
    ))

    assert mediaserver.not_exists(media_in=MediaInfo(title="测试电影", type=type_name)) == []
    assert missing.call_args.kwargs["meta"].type is expected
    assert missing.call_args.kwargs["mediainfo"].type is (
        expected if type_name is not None else None
    )


@pytest.mark.parametrize("type_name,expected", MEDIA_TYPES + [
    (None, None), ("", None), ("自动", None), ("auto", None), ("none", None),
])
def test_manual_transfer_forwards_normalized_type(monkeypatch, type_name, expected) -> None:
    """手动整理兼容中英文类型，并保留未指定和自动识别语义。"""
    execute = Mock(return_value=(True, ""))
    monkeypatch.setattr(transfer, "TransferChain", lambda: SimpleNamespace(manual_transfer=execute))

    result = transfer.manual_transfer(
        transer_item=ManualTransferItem(
            fileitem=FileItem(storage="local", path="/downloads/test.mkv", type="file"),
            type_name=type_name,
        ),
        background=True,
        history_query=Mock(),
    )

    assert result.success is True
    assert execute.call_args.kwargs["mtype"] is expected


def test_manual_transfer_rejects_invalid_type_before_execution(monkeypatch) -> None:
    """非法媒体类型应保持失败响应，不能启动实际整理。"""
    chain_factory = Mock()
    monkeypatch.setattr(transfer, "TransferChain", chain_factory)

    result = transfer.manual_transfer(
        transer_item=ManualTransferItem(
            fileitem=FileItem(storage="local", path="/downloads/test.mkv", type="file"),
            type_name="unsupported",
        ),
        background=True,
        history_query=Mock(),
    )

    assert result.success is False
    assert result.message == "不支持的媒体类型：unsupported"
    chain_factory.assert_not_called()
