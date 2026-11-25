"""
验证订阅、整理及资源链在缺失媒体或文件元数据时停止写入并保留有效结果。
"""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.subscription.contract import SubscriptionSnapshot
from app.application.torrent.download import TorrentHelper
from app.chain import _music
from app.chain._messaging import _render_notification_message
from app.chain.interaction import MediaInteractionChain
from app.chain.storage import StorageChain
from app.chain.subscribe.completion import SubscribeCompletionOwner
from app.chain.subscribe.create import SubscribeCreateOwner
from app.chain.subscribe.facade import SubscribeChain
from app.chain.subscribe.match import _prepare_match_torrents
from app.chain.torrents import TorrentsChain
from app.chain.transfer.request import _TransferCandidatePlanner
from app.chain.transfer.workflow import TransferWorkflowOwner
from app.domain.context import Context, MediaInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.schemas.file import FileItem
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType, NotificationChannel


@pytest.mark.parametrize("method_name", [
    "_SubscribeChain__prepare_subscribe_episodes", "_SubscribeChain__async_prepare_subscribe_episodes",
    "_SubscribeChain__finalize_subscribe_create_context", "_SubscribeChain__async_finalize_subscribe_create_context",
    "_SubscribeChain__apply_subscribe_create_defaults", "_SubscribeChain__persist_subscribe_create",
    "_SubscribeChain__async_persist_subscribe_create",
])
def test_subscription_stages_without_media_do_not_write(method_name):
    """
    阶段边界接收到空识别结果时返回失败或停止补图，不访问仓储和通知端口。
    """
    owner = object.__new__(SubscribeCreateOwner)
    context = SimpleNamespace(mediainfo=None)
    result = getattr(owner, method_name)(context)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    if "persist" in method_name:
        assert result == (None, "未识别到媒体信息")
    elif "prepare" in method_name:
        assert result == "未识别到媒体信息"
    else:
        assert result is None


def test_subscription_cannot_freeze_post_commit_context_without_media():
    """
    不完整识别结果不能进入提交后的事件、通知与统计阶段。
    """
    with pytest.raises(ValueError, match="缺少媒体信息"):
        SubscribeCreateOwner._SubscribeChain__subscribe_post_commit_context(SimpleNamespace(mediainfo=None), None)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_episode_refresh_losing_media_is_a_creation_error(asynchronous, monkeypatch):
    """
    补齐季集的二次识别失败时返回业务错误，不能继续解引用旧媒体或提交订阅。
    """
    media = MediaInfo(type=MediaType.TV, media_source=MediaSource.TMDB, media_id="1")
    context = SimpleNamespace(mediainfo=media, season=None, options={}, episode_group=None)
    chain = SimpleNamespace(recognize_media=Mock(return_value=None), async_recognize_media=AsyncMock(return_value=None))
    monkeypatch.setattr("app.chain.subscribe.create.MediaChain", Mock(return_value=chain))
    owner = object.__new__(SubscribeCreateOwner)
    method = owner._SubscribeChain__async_prepare_subscribe_episodes if asynchronous else owner._SubscribeChain__prepare_subscribe_episodes
    result = method(context)
    assert (asyncio.run(result) if asynchronous else result) == "未识别到媒体信息"
    assert context.mediainfo is None


def test_movie_priority_ignores_download_context_without_torrent():
    """
    缺少种子信息的下载上下文不覆盖现有电影优先级。
    """
    owner = object.__new__(SubscribeCompletionOwner)
    subscribe = SubscriptionSnapshot(id=1, name="Demo", type=MediaType.MOVIE.value, current_priority=80)
    result = owner._SubscribeChain__update_movie_download_priority(subscribe, MediaInfo(), [Context()])
    assert result is subscribe
    assert result.current_priority == 80


def test_missing_season_scope_subtracts_confirmed_downloads():
    """
    季范围未包含目标季时仍按订阅总集数扣除已下载集，不能丢失下载事实。
    """
    missing = {"tmdb:1": {2: NotExistMediaInfo(season=2, episodes=[1, 2], total_episode=2)}}
    complete, result = SubscribeCompletionOwner._SubscribeChain__get_subscribe_no_exits(
        "剧集", missing, "tmdb:1", 1, 3, None, [1],
    )
    assert complete is False
    assert set(result["tmdb:1"][1].episodes) == {2, 3}


@pytest.mark.parametrize("media_type", [MediaType.MOVIE, MediaType.TV])
def test_subscribe_file_projection_initializes_download_and_library_lists(media_type, monkeypatch):
    """
    电影和电视剧的首次文件记录会建立独立列表，同时保留下载与媒体库来源。
    """
    tv = media_type == MediaType.TV
    filename = "Demo.S01E01.mkv" if tv else "Demo.2026.mkv"
    subscribe = SubscriptionSnapshot(id=1, name="Demo", type=media_type.value, year="2026", season=1 if tv else None,
                                     media_source="douban", media_id="1", start_episode=1, total_episode=2)
    history = SimpleNamespace(download_hash="hash", torrent_name="torrent", torrent_site="site")
    file = SimpleNamespace(filepath=filename, downloader="qb", fullpath=f"/downloads/{filename}")
    repository = SimpleNamespace(get_by_media_identity=Mock(return_value=[history]), get_files_by_hash=Mock(return_value=[file]))
    chain = SubscribeChain()
    monkeypatch.setattr(chain, "download_history_repository", repository)
    monkeypatch.setattr(chain, "media_files", Mock(return_value=[FileItem(storage="local", type="file", path=f"/library/{filename}")]))
    monkeypatch.setattr(chain, "_append_subscribe_media_servers", Mock())
    media_chain = SimpleNamespace(recognize_media=Mock(return_value=MediaInfo(type=media_type, title="Demo")))
    monkeypatch.setattr("app.chain.subscribe.query.MediaChain", Mock(return_value=media_chain))
    result = chain.subscribe_files_info(subscribe)
    episode = result.episodes[1 if tv else 0]
    assert [item.file_path for item in episode.download] == [f"/downloads/{filename}"]
    assert [item.file_path for item in episode.library] == [f"/library/{filename}"]
    assert episode.download[0].hash == "hash"


def test_total_episode_decrease_without_season_keeps_candidate(monkeypatch):
    """
    未指定季的旧电视剧订阅不推断其他季的缺集事实来阻止集数更新。
    """
    owner = SubscribeChain()
    monkeypatch.setattr(owner, "_SubscribeChain__is_full_best_version_enabled", Mock(return_value=False))
    subscribe = SubscriptionSnapshot(id=1, name="Demo", type=MediaType.TV.value, total_episode=12, season=None)
    assert owner._SubscribeChain__resolve_total_episode_decrease(subscribe, 10, MetaInfo("Demo"), MediaInfo()) == 10


def test_incomplete_resource_is_not_included_in_grouped_downloads(monkeypatch):
    """
    缺少元数据或媒体识别结果的资源不能参与按媒体名称分组去重。
    """
    helper = TorrentHelper()
    contexts = [Context(), Context(meta_info=MetaInfo("Demo"))]
    monkeypatch.setattr(helper, "sort_torrents", Mock(return_value=contexts))
    assert helper.sort_group_torrents(contexts) == []


def test_storage_and_transfer_require_source_path(monkeypatch):
    """
    路径缺失时存储查询与整理候选均停止，不把默认目录当作源文件。
    """
    item = FileItem(storage="local", type="file", path=None)
    storage = StorageChain()
    lookup = Mock(side_effect=AssertionError("缺少路径不应查询存储"))
    monkeypatch.setattr(storage, "get_file_item", lookup)
    assert storage.get_item(item) is None
    owner = object.__new__(TransferWorkflowOwner)
    assert owner._TransferChain__get_trans_fileitems(item, None, verify_file_exists=False) == []
    planner = object.__new__(_TransferCandidatePlanner)
    planner._continue_callback = None
    assert planner._is_allowed_transfer_item(item, False) is False
    lookup.assert_not_called()


def test_music_subscription_missing_type_defaults_to_recording():
    """
    旧音乐订阅缺少实体类型时恢复为单曲，原名回写仍保留可空回退语义。
    """
    subscribe = SubscriptionSnapshot(id=1, name="歌名", type=MediaType.MUSIC.value, music_type=None)
    assert _music._music_info_from_subscribe(subscribe).music_type == "recording"
    meta = MetaMusic("原始歌名")
    meta.original_name = "回写歌名"
    assert meta.original_name == "回写歌名"
    meta.original_name = None
    assert meta.org_string is None


@pytest.mark.parametrize("missing_key", ["url", "cookie", "rss"])
def test_expired_rss_without_required_site_fields_does_not_request(missing_key, monkeypatch):
    """
    站点配置不完整时不生成续期请求，避免把无效参数交给认证与 RSS 服务。
    """
    site = {"url": "https://site.test", "cookie": "cookie", "rss": "https://site.test/rss"}
    site[missing_key] = None
    request = Mock(side_effect=AssertionError("配置不完整不应续期"))
    monkeypatch.setattr("app.chain.torrents.RssHelper", request)
    object.__new__(TorrentsChain)._TorrentsChain__renew_rss_url("site.test", site)
    request.assert_not_called()


def test_missing_notification_template_does_not_render_message():
    """
    缺少消息模板时不把空值交给渲染器，也不生成空通知。
    """
    assert _render_notification_message(message=None, meta=None, mediainfo=None, torrentinfo=None,
                                        transferinfo=None, kwargs={}) is None


def test_subscription_preparation_preserves_incomplete_context_without_recognition():
    """
    缺少种子或元数据的上下文保留给后续判定，不启动媒体识别或累计失败。
    """
    context = Context()
    result = _prepare_match_torrents({"site": [context]})
    assert result == {"site": [context]}
    assert context.media_recognize_fail_count == 0


def test_torrent_buttons_skip_missing_resource():
    """
    空候选不生成下载按钮，自动选择按钮仍保留有效交互请求标识。
    """
    owner = object.__new__(MediaInteractionChain)
    buttons = owner._create_torrent_buttons(NotificationChannel.Telegram,
                                            SimpleNamespace(request_id="request"), [Context()], 1, 1)
    assert len(buttons) == 1
    assert buttons[0][0]["callback_data"] == "media:request:download:0"
