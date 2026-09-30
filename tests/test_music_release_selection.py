"""人工选定具体发行的HTTP、来源核验和整理预览/执行合同。"""

import asyncio
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.adapters.web.security.access import verify_token
from app.api.endpoints import music as music_endpoint
from app.api.endpoints.transfer import manual_transfer
from app.chain.media import MediaChain
from app.chain.musicbrainz import MusicBrainzChain
from app.chain.theaudiodb import TheAudioDbChain
from app.chain.transfer.facade import TransferChain
from app.domain.context import MusicAlbumInfo, MusicInfo
from app.modules.musicbrainz import MusicBrainzModule
from app.schemas.transfer import ManualTransferItem, TransferInfo
from app.schemas.types import MediaSource, MediaType
from tests.test_music_resource_context import _audio_files
from tests.test_transfer_music_album_selection import _directory_item, _prepare_chain
from tests.test_transfer_sync_extra_files import make_fileitem, make_transfer_chain

GROUP = "a1111111-1111-4111-8111-111111111111"
STANDARD = "b2222222-2222-4222-8222-222222222222"
DELUXE = "c3333333-3333-4333-8333-333333333333"


def _payloads():
    """发行组内嵌列表只展示标准版，豪华版需直查且包含不同曲目与日期。"""
    group = {"id": GROUP, "title": "Album", "primary-type": "Album", "first-release-date": "2004",
             "artist-credit": [{"artist": {"name": "Artist"}}],
             "releases": [{"id": STANDARD, "status": "Official", "date": "2004"}]}
    release = {"id": DELUXE, "title": "Album (Deluxe)", "date": "2020", "country": "JP",
               "release-group": {"id": GROUP}, "media": [{"position": 1, "track-count": 2, "tracks": [
                   {"id": f"release-track-{i}", "position": i, "title": title, "length": 3000,
                    "recording": {"id": f"recording-{i}", "title": title}}
                   for i, title in enumerate(("First Song", "Second Song"), 1)
               ]}]}
    return group, release


def _module(monkeypatch, group, release):
    """隔离唯一HTTP边界，同步/异步均走真实MusicBrainz投影及版本选择。"""
    calls = []

    def request(path, params=None):
        """记录请求并拒绝未声明的回退、别名或其它外网访问。"""
        calls.append((path, params))
        assert path in {f"/release-group/{GROUP}", f"/release/{DELUXE}"}
        return deepcopy(group if path.startswith("/release-group/") else release)

    monkeypatch.setattr(MusicBrainzModule, "_request_json", staticmethod(request))
    monkeypatch.setattr(MusicBrainzModule, "_async_request_json", AsyncMock(side_effect=request))
    return MusicBrainzModule(), calls


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("invalid", [None, "group", "release", "membership", "missing", "empty_tracks"])
def test_explicit_release_is_verified_without_default_fallback(monkeypatch, asynchronous, invalid):
    """缺失列表不妨碍直查；错误实体/归属/服务空响应均不能回退默认版。"""
    group, release = _payloads()
    if invalid == "group":
        group["id"] = STANDARD
    elif invalid == "release":
        release["id"] = STANDARD
    elif invalid == "membership":
        release["release-group"] = {"id": STANDARD}
    elif invalid == "missing":
        release = None
    elif invalid == "empty_tracks":
        release["media"] = []
    module, calls = _module(monkeypatch, group, release)
    kwargs = dict(media_source=MediaSource.MusicBrainz, media_id=GROUP, musicbrainz_release_id=DELUXE.upper(),
                  music_release_regions=["CN"], music_release_scripts=["Hans"])
    album = asyncio.run(module._async_music_album(**kwargs)) if asynchronous else module.music_album(**kwargs)
    if invalid:
        assert album is None
    else:
        assert (album.media_id, album.musicbrainz_release_id, album.title, album.year) == (GROUP, DELUXE, "Album (Deluxe)", 2020)
        assert [track.media_id for track in album.tracks] == ["recording-1", "recording-2"]
        assert all(track.musicbrainz_release_id == DELUXE and track.album_id == GROUP for track in album.tracks)
        assert [track.musicbrainz_release_track_id for track in album.tracks] == ["release-track-1", "release-track-2"]
    assert len(calls) == (1 if invalid == "group" else 2)
    if len(calls) == 2:
        assert "release-groups" in calls[1][1]["inc"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("returned_release", [STANDARD, None, DELUXE])
def test_source_chain_rejects_provider_ignoring_explicit_edition(monkeypatch, asynchronous, returned_release):
    """旧插件忽略新入参而返回默认版时必须失败，不能静默把它当成人工选择。"""
    chain = MusicBrainzChain()
    album = MusicAlbumInfo(media_source=MediaSource.MusicBrainz, media_id=GROUP,
                           musicbrainz_release_id=returned_release, musicbrainz_release_group_id=GROUP,
                           tracks=[MusicInfo(media_source=MediaSource.MusicBrainz, media_id="recording", album_id=GROUP,
                                             musicbrainz_release_group_id=GROUP, musicbrainz_release_id=returned_release)])
    dispatch = AsyncMock(return_value=album) if asynchronous else Mock(return_value=album)
    monkeypatch.setattr(chain, "async_run_module" if asynchronous else "run_module", dispatch)
    result = (asyncio.run(chain.async_get_music_album(GROUP, musicbrainz_release_id=DELUXE.upper())) if asynchronous
              else chain.get_music_album(GROUP, musicbrainz_release_id=DELUXE.upper()))
    assert (result is album) == (returned_release == DELUXE)
    assert dispatch.call_args.kwargs["musicbrainz_release_id"] == DELUXE


def test_old_album_lookup_omits_new_keyword_and_other_sources_refuse_it(monkeypatch):
    """未使用新能力的来源签名保持兼容，显式MB发行不跨源解释。"""
    for chain in (MusicBrainzChain(), TheAudioDbChain()):
        dispatch = Mock(return_value=None)
        monkeypatch.setattr(chain, "run_module", dispatch)
        assert chain.get_music_album(GROUP) is None
        assert dispatch.call_args.kwargs == {"media_source": chain.source, "media_id": GROUP}
    dispatch.reset_mock()
    assert chain.get_music_album(GROUP, musicbrainz_release_id=DELUXE) is None
    dispatch.assert_not_called()


@pytest.mark.parametrize("changes", [{"media_source": "theaudiodb"}, {"media_id": None}, {"music_type": "recording"},
                                    {"music_type": None}, {"type_name": "电影"}, {"musicbrainz_release_id": "../bad"}])
def test_manual_request_rejects_conflicting_release_namespace(changes):
    """请求Schema直接拒绝跨来源、录音或影视身份组合与非法发行ID。"""
    values = dict(media_source="musicbrainz", media_id=GROUP, music_type="album", musicbrainz_release_id=DELUXE)
    with pytest.raises(ValidationError):
        ManualTransferItem(**(values | changes))


@pytest.mark.anyio
async def test_album_http_preserves_edition_and_rejects_wrong_source(monkeypatch):
    """实际HTTP校验并传递具体发行ID，旧请求不新增关键字，错误归属返回404。"""
    group, release = _payloads()
    module, _calls = _module(monkeypatch, group, release)
    album = module.music_album(MediaSource.MusicBrainz, GROUP, musicbrainz_release_id=DELUXE)
    lookup = AsyncMock(return_value=album)
    monkeypatch.setattr(music_endpoint, "MediaChain", lambda: SimpleNamespace(async_get_music_album=lookup))
    app = FastAPI()
    app.include_router(music_endpoint.router, prefix="/music")
    app.dependency_overrides[verify_token] = lambda: "test"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/music/album/{GROUP}", params={"musicbrainz_release_id": DELUXE})
        assert response.status_code == 200
        assert response.json()["data"]["musicbrainz_release_id"] == DELUXE
        assert lookup.call_args.kwargs["musicbrainz_release_id"] == DELUXE
        await client.get(f"/music/album/{GROUP}")
        assert "musicbrainz_release_id" not in lookup.call_args.kwargs
        lookup.reset_mock()
        for params in ({"musicbrainz_release_id": "bad"}, {"musicbrainz_release_id": DELUXE, "media_source": "theaudiodb"}):
            assert (await client.get(f"/music/album/{GROUP}", params=params)).status_code == 422
        lookup.assert_not_called()
        lookup.return_value = None
        assert (await client.get(f"/music/album/{GROUP}", params={"musicbrainz_release_id": DELUXE})).status_code == 404


@pytest.mark.parametrize("selected_files", [False, True])
def test_manual_edition_preview_execution_and_replay_keep_same_tracks(tmp_path, monkeypatch, selected_files):
    """真实FLAC经API到曲目规划，预览与执行同版；冻结任务重放不另选标准版或改源文件。"""
    paths = _audio_files(tmp_path / "Artist - Album")
    original = [sha256(path.read_bytes()).hexdigest() for path in paths]
    items = [make_fileitem(str(path)) for path in paths]
    chain = _prepare_chain(monkeypatch, items)
    monkeypatch.setattr("app.chain.transfer.workflow.StorageChain.get_item", lambda _self, item: item)
    group, release = _payloads()
    module, _calls = _module(monkeypatch, group, release)
    metadata = MediaChain()
    source = MusicBrainzChain()
    monkeypatch.setattr(source, "run_module", lambda _method, **kwargs: module.music_album(**kwargs))
    monkeypatch.setattr(metadata, "_music_source_chain", lambda _source: source)
    monkeypatch.setattr(metadata, "_finalize_recognition_result", lambda result: result)
    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", lambda: chain)
    planned = []

    def execute(task, callback=None):
        """在真实规划后的文件操作边界记录版本，不接触生产存储。"""
        planned.append((task.preview, task.mediainfo.to_dict(), task.planning_input))
        if task.preview and callback:
            callback(task, TransferInfo(success=True))
        return True, ""

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", execute)
    for preview in (True, False):
        request = ManualTransferItem(fileitem=_directory_item(paths[0].parent), fileitems=items if selected_files else None,
                                     media_source="musicbrainz", media_id=GROUP, music_type="album", type_name="音乐",
                                     musicbrainz_release_id=DELUXE.upper(), preview=preview)
        response = manual_transfer(request, background=False, history_query=SimpleNamespace(), _="test")
        assert response.success, response.message
    assert len(planned) == 4
    assert [entry[1]["title"] for entry in planned] == ["First Song", "Second Song"] * 2
    assert all(entry[1]["musicbrainz_release_id"] == DELUXE for entry in planned)
    restored = []
    replay_owner = make_transfer_chain()
    monkeypatch.setattr(replay_owner, "_TransferChain__bind_claimed_admission", lambda *_args: None)
    monkeypatch.setattr(replay_owner, "put_to_queue", lambda task: restored.append(task) or True)
    for item, (_preview, info, planning) in zip(items, planned[2:]):
        admission = SimpleNamespace(planning_input=planning, task_id="replay-selected")
        assert replay_owner._TransferChain__queue_accepted_replay(item, admission)
        assert restored[-1].mediainfo.musicbrainz_release_id == info["musicbrainz_release_id"]
        assert restored[-1].mediainfo.media_id == info["media_id"]
    assert [sha256(path.read_bytes()).hexdigest() for path in paths] == original


def test_invalid_manual_release_cannot_enqueue_or_fall_back(monkeypatch):
    """旧调用绕过HTTP时同样拒绝不完整发行身份，查不到发行也不进入文件整理。"""
    owner = make_transfer_chain()
    execute = Mock()
    monkeypatch.setattr(owner, "do_transfer", execute)
    lookup = Mock(return_value=None)
    monkeypatch.setattr("app.chain.transfer.history.MediaChain", lambda: SimpleNamespace(get_music_album=lookup))
    item = make_fileitem("/music/song.flac")
    assert not owner.manual_transfer(item, musicbrainz_release_id=DELUXE)[0]
    lookup.assert_not_called()
    assert not owner.manual_transfer(item, media_source=MediaSource.MusicBrainz, media_id=GROUP,
                                     music_type="album", mtype=MediaType.MUSIC, musicbrainz_release_id=DELUXE)[0]
    execute.assert_not_called()


def test_explicit_release_remote_storage_fails_before_lookup(monkeypatch):
    """远端无法读取曲目证据时明确拒绝指定发行，防止退回逐文件默认识别。"""
    owner = make_transfer_chain()
    lookup = Mock()
    monkeypatch.setattr("app.chain.transfer.history.MediaChain", lookup)
    remote = make_fileitem("/music/01.flac").model_copy(update={"storage": "alist"})
    state, message = owner.manual_transfer(remote, media_source=MediaSource.MusicBrainz, media_id=GROUP,
                                           music_type="album", musicbrainz_release_id=DELUXE)
    assert not state and "本地" in message
    lookup.assert_not_called()


def test_explicit_release_cue_keeps_album_identity_and_companion(tmp_path, monkeypatch):
    """整轨按CUE逻辑曲目核验所选版，音频和索引仍作为同一专辑，不伪造单曲身份。"""
    from tests.test_music_cue import CUE, _image_pair

    cue_text = CUE.replace("测试歌手", "Artist").replace("专辑示例", "Album")
    cue_text = cue_text.replace("第一首歌曲", "First Song").replace("第二首歌曲", "Second Song")
    audio, cue = _image_pair(tmp_path, text=cue_text)
    items = [make_fileitem(str(path)) for path in (audio, cue)]
    before = [sha256(path.read_bytes()).hexdigest() for path in (audio, cue)]
    chain = _prepare_chain(monkeypatch, items)
    group, release = _payloads()
    tracks = release["media"][0]["tracks"]
    tracks[0]["length"], tracks[1]["length"] = 1000, 2000
    module, _calls = _module(monkeypatch, group, release)
    metadata = MediaChain()
    source = MusicBrainzChain()
    monkeypatch.setattr(source, "run_module", lambda _method, **kwargs: module.music_album(**kwargs))
    monkeypatch.setattr(metadata, "_music_source_chain", lambda _source: source)
    monkeypatch.setattr(metadata, "_finalize_recognition_result", lambda result: result)
    planned = []

    def capture(task, callback=None):
        """在文件处理边界核验音频与配套文件共享Album身份。"""
        planned.append(task)
        callback(task, TransferInfo(success=True))
        return True, ""

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", capture)
    state, result = TransferChain.manual_transfer(chain, _directory_item(audio.parent), media_source=MediaSource.MusicBrainz,
                                                  media_id=GROUP, music_type="album", musicbrainz_release_id=DELUXE,
                                                  preview=True, background=False)
    assert state, result
    assert len(planned) == 2
    assert all(task.mediainfo.music_type == "album" and task.mediainfo.media_id == GROUP for task in planned)
    assert all(task.mediainfo.musicbrainz_release_id == DELUXE for task in planned)
    assert [sha256(path.read_bytes()).hexdigest() for path in (audio, cue)] == before


@pytest.mark.parametrize("field,value", [("musicbrainz_release_id", STANDARD), ("musicbrainz_release_group_id", STANDARD),
                                       ("album_id", STANDARD), ("media_source", "theaudiodb")])
def test_explicit_album_rejects_mixed_provider_track_identities(monkeypatch, field, value):
    """插件返回的专辑与曲目必须来自同一个发行版，不因专辑外壳ID正确而放行混合数据。"""
    group, release = _payloads()
    module, _calls = _module(monkeypatch, group, release)
    album = module.music_album(MediaSource.MusicBrainz, GROUP, musicbrainz_release_id=DELUXE)
    setattr(album.tracks[0], field, value)
    chain = MusicBrainzChain()
    monkeypatch.setattr(chain, "run_module", Mock(return_value=album))
    assert chain.get_music_album(GROUP, musicbrainz_release_id=DELUXE) is None
