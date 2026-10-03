"""音乐预览保留真实证据与发行边界，不把ID、可读性和文件成功混成同一状态。"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from mutagen.flac import FLAC

from app.api.endpoints.transfer import manual_transfer
from app.application.transfer.models import TransferTask
from app.chain.transfer.music import MusicReleaseGroup, _music_preview_context
from app.chain.transfer.request import build_transfer_preview_item
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.schemas.transfer import ManualTransferItem, ManualTransferPreviewItem, TransferInfo
from app.schemas.types import MediaType
from tests.test_music_cue import _image_pair
from tests.test_music_release_groups import RELEASE_A, RELEASE_B, _audio, _prepare, _resolve
from tests.test_transfer_sync_extra_files import make_fileitem


def _task(meta=None, info=None):
    """构造无文件I/O的音乐预览任务，录音身份始终保持独立。"""
    meta = meta or MetaMusic("Artist - Song.flac")
    return TransferTask(fileitem=make_fileitem("/downloads/Record/Song.flac"), meta=meta, mediainfo=info, preview=True)


def _preview(task, success=True):
    """走实际响应投影及公开Schema，验证新增字段不会被传输层丢弃。"""
    return ManualTransferPreviewItem.model_validate(build_transfer_preview_item(
        task, TransferInfo(success=success, message="分类没有匹配" if not success else ""),
    ))


@pytest.mark.parametrize("status", ["not_found", "ambiguous", "conflict", "service_error", "budget_exhausted"])
def test_pending_diagnostic_cannot_become_online_success(status):
    """不完整来源返回即使带ID或有本次手选也不能覆盖真实冲突和故障。"""
    info = MusicInfo(media_id="recording", media_source="musicbrainz", title="Song",
                     raw_data={"recognition": {"status": status}})
    task = _task(info=info)
    task.manual, task.media_source, task.media_id = True, info.media_source, info.media_id
    result = _preview(task, False)
    assert result.music.status == status
    assert not result.music.online_confirmed


@pytest.mark.parametrize("origin,status", [("tag", "metadata"), ("torrent", "metadata"),
                                         ("manual", "manual"), ("remote", "matched"), (None, "metadata")])
def test_identity_origin_does_not_invent_online_confirmation(origin, status):
    """已有标签或旧历史ID缺少在线核验记录时只显示已有信息。"""
    meta = MetaMusic("Artist - Song.flac")
    meta.title = "Song"
    meta.media_id, meta.media_source = "recording", "musicbrainz"
    if origin:
        meta.field_sources["media_id"] = origin
    result = _preview(_task(meta, MusicInfo.from_meta(meta)), False)
    assert not result.success
    assert result.music.status == status
    assert result.music.online_confirmed == (status == "matched")


def test_actual_online_match_and_explicit_selection_remain_distinct():
    """在线核验不依赖执行成功，本次手选则显示人工选择而不是自动匹配。"""
    info = MusicInfo(media_id="recording", media_source="musicbrainz", title="Song",
                     raw_data={"recognition": {"status": "matched"}})
    task = _task(info=info)
    assert _preview(task, False).music.status == "matched"
    task.manual, task.media_source, task.media_id = True, info.media_source, info.media_id
    result = _preview(task)
    assert result.music.status == "manual"
    assert not result.music.online_confirmed


def test_preview_crops_diagnostics_and_keeps_identity_namespaces():
    """候选摘要有上限、无原始响应/评分，Release不能冒充Recording或Release Group。"""
    candidates = [{"title": f"Album {index}", "release_id": RELEASE_A, "album_id": RELEASE_B,
                   "year": 2003, "score": 115, "raw": {"token": "private"}, "url": "secret"}
                  for index in range(8)]
    info = MusicInfo(title="Song", raw_data={"recognition": {"status": "ambiguous", "candidates": candidates}})
    info.field_sources = {"title": "tag", "album": "remote", "private": "secret"}
    result = _preview(_task(info=info))
    assert len(result.music.candidates) == 5
    assert result.music.candidates[0].media_id is None
    assert result.music.candidates[0].release_id == RELEASE_A
    assert result.music.candidates[0].album_id == RELEASE_B
    assert result.music.candidates[0].year == "2003"
    assert result.music.field_sources["title"] == "tag"
    assert "private" not in result.model_dump_json() and "secret" not in result.model_dump_json()
    assert "score" not in result.model_dump_json()
    assert len(info.raw_data["recognition"]["candidates"]) == 8


def test_tagged_groups_are_stable_and_separate_same_directory_releases(tmp_path):
    """同目录同名专辑有两个发行ID时，预览不能把两版一起纠正。"""
    items = [_audio(tmp_path / f"Song {index}.flac", release=release)
             for index, release in enumerate((RELEASE_A, RELEASE_A, RELEASE_B))]
    owner, context = _prepare(items)
    details = []
    for item in items:
        meta, info = _resolve(owner, context, item)
        task = TransferTask(fileitem=item, meta=meta, mediainfo=info, preview=True)
        task.bind_music_preview_context(_music_preview_context(owner, task, context))
        details.append(_preview(task).music)
    assert details[0].group_id == details[1].group_id != details[2].group_id
    assert [detail.group_size for detail in details] == [2, 2, 1]
    reversed_group = MusicReleaseGroup(tmp_path, tuple(reversed(items[:2])), None)
    assert reversed_group.preview_id == details[0].group_id
    remote = items[0].model_copy(update={"storage": "alist"})
    assert MusicReleaseGroup(tmp_path, (remote, items[1]), None).preview_id != details[0].group_id


def test_readability_does_not_confuse_untagged_corrupt_and_remote(tmp_path):
    """实际无标签音频、损坏文件与远端同名路径分别显示不同读证据状态。"""
    empty = tmp_path / "01.flac"
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", empty)
    corrupt = tmp_path / "02.flac"
    corrupt.write_bytes(b"broken")
    items = [make_fileitem(str(empty)), make_fileitem(str(corrupt)),
             make_fileitem(str(empty)).model_copy(update={"storage": "alist"})]
    owner, context = _prepare(items)
    actual = [_music_preview_context(owner, TransferTask(fileitem=item, meta=MetaMusic(item.name)), context)["read_status"]
              for item in items]
    assert actual == ["stream_only", "unreadable", "name_only"]


def test_cue_companion_shares_image_group_and_local_evidence(tmp_path):
    """整轨与CUE同组但角色不同，按组纠正能保留配套索引且不伪装单曲在线确认。"""
    audio, cue = _image_pair(tmp_path)
    items = [make_fileitem(str(path)) for path in (audio, cue)]
    owner, context = _prepare(items)
    rows = []
    for item in items:
        meta, info = _resolve(owner, context, item)
        task = TransferTask(fileitem=item, meta=meta, mediainfo=info, preview=True)
        task.bind_music_preview_context(_music_preview_context(owner, task, context))
        rows.append(_preview(task).music)
    assert rows[0].status == rows[1].status == "local_cue"
    assert rows[0].group_id == rows[1].group_id
    assert rows[0].layout == rows[1].layout == "image_cue"
    assert rows[1].read_status == rows[1].file_role == "companion"
    assert not any(row.online_confirmed for row in rows)


def test_transient_preview_context_does_not_enter_task_snapshots():
    """展示分组不进入旧插件对象/持久输入；响应提供原存储ID但不泄露临时下载URL。"""
    task = _task()
    task.fileitem = task.fileitem.model_copy(update={"storage": "alist", "fileid": "opaque",
                                                  "url": "https://example.test/?token=private"})
    task.bind_music_preview_context({"group_id": "group", "read_status": "name_only"})
    changed = task.music_preview_context
    changed["group_id"] = "changed"
    assert task.music_preview_context["group_id"] == "group"
    assert "music_preview_context" not in str(task.to_dict())
    assert "music_preview_context" not in task.model_dump()
    result = _preview(task)
    assert result.source_item.fileid == "opaque"
    assert result.source_item.url is None
    assert result.source_storage == "alist"
    del task.__pydantic_private__["_music_preview_context"]
    assert _preview(task).music.read_status == "unknown"


def test_real_tag_recording_id_is_local_until_actually_verified(tmp_path):
    """真实FLAC带Recording ID仍走标签快速路径，不能因ID非空改标在线命中。"""
    item = _audio(tmp_path / "Song.flac")
    audio = FLAC(item.path)
    audio["musicbrainz_trackid"] = RELEASE_A
    audio.save()
    owner, context = _prepare([item])
    meta, info = _resolve(owner, context, item)
    result = _preview(TransferTask(fileitem=item, meta=meta, mediainfo=info))
    assert result.music.status == "local_tags"
    assert result.music.media_id == RELEASE_A
    assert not result.music.online_confirmed


def test_api_keeps_same_path_from_different_storages(monkeypatch):
    """API合并多个目录预览时以存储加路径区分源文件，保留后端提供的纠正范围。"""
    roots = [make_fileitem("/downloads/Record").model_copy(update={"storage": storage, "type": "dir"})
             for storage in ("local", "alist")]

    def preview(**kwargs):
        """模拟链边界响应，后续真实API Schema投影与去重仍执行。"""
        item = make_fileitem("/downloads/Record/Song.flac").model_copy(update={"storage": kwargs["fileitem"].storage})
        task = _task()
        task.fileitem = item
        task.bind_music_preview_context({"group_id": item.storage, "read_status": "unknown"})
        row = build_transfer_preview_item(task, TransferInfo(success=True))
        return True, {"items": [row, row], "summary": {"total": 2, "success": 2, "failed": 0}}

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", lambda: SimpleNamespace(manual_transfer=preview))
    response = manual_transfer(transer_item=ManualTransferItem(fileitems=roots, preview=True, type_name="音乐"),
                               background=False, history_query=SimpleNamespace(), _="token")
    assert response.success
    data = response.model_dump()["data"]
    assert data["summary"] == {"total": 2, "success": 2, "failed": 0}
    assert [item["source_storage"] for item in data["items"]] == ["local", "alist"]
    assert [item["music"]["group_id"] for item in data["items"]] == ["local", "alist"]


def test_video_preview_keeps_music_details_empty():
    """影视字幕/音轨上下文不被本次展示契约强行转换为音乐。"""
    task = _task(MetaBase("Movie.mkv"), MediaInfo(title="Movie", type=MediaType.MOVIE))
    assert _preview(task).music is None
