from unittest.mock import Mock, patch

from app.api.endpoints.download import add, download
from app.application.orchestration.download import DownloadChain
from app.domain.context import MUSIC_ENTITY_ALBUM, Context, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.schemas import ExistMediaInfo
from app.schemas.context import TorrentInfo
from app.schemas.music import MusicInfo as MusicInfoSchema
from app.schemas.types import MediaType


def _music_info() -> MusicInfo:
    """构造下载测试使用的标准音乐信息。"""
    return MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        year=2003,
        cover_url="https://example.com/cover.jpg",
        raw_data={"large": "payload"},
    )


def _album_info(total_tracks: int | None = 3) -> MusicInfo:
    """构造整张专辑下载校验使用的目标信息。"""
    return MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
        title="叶惠美",
        album="叶惠美",
        artists=["周杰伦"],
        total_tracks=total_tracks,
    )


def test_music_info_exposes_download_chain_unified_identity():
    """音乐下载上下文仅使用统一身份，并保留无季集的领域约束。"""
    info = _music_info()
    meta = MetaMusic.from_music_info(info)

    assert info.type == MediaType.MUSIC
    assert info.media_source.value == "musicbrainz"
    assert info.media_id == "recording-1"
    assert info.episode_group is None
    assert meta.episode_list == []
    assert meta.season_episode == ""


def test_download_note_keeps_versioned_music_context():
    """音乐下载历史备注应保存可恢复且不含上游原始大对象的上下文。"""
    info = _music_info()
    meta = MetaMusic.from_music_info(info)

    note = DownloadChain._build_download_note("Manual", info, meta)

    assert note["source"] == "Manual"
    assert note["music"]["version"] == 1
    assert note["music"]["meta"]["album"] == "叶惠美"
    assert note["music"]["media"]["media_id"] == "recording-1"
    assert "raw_data" not in note["music"]["media"]


def test_album_resource_requires_all_independent_audio_tracks():
    """整专资源只有在独立音频文件数覆盖专辑曲目数时才可标记完整。"""
    context = Context(media_info=_album_info(total_tracks=3))

    error = DownloadChain._validate_music_album_resource(
        context,
        ["叶惠美/01.flac", "叶惠美/02.flac", "叶惠美/03.m4a", "叶惠美/cover.jpg"],
    )

    assert error is None
    assert context.confirmed_full_coverage is True


def test_album_resource_rejects_incomplete_or_unverifiable_pack():
    """曲目不足、未知曲目总数或无文件清单时不得把专辑订阅判定为完成。"""
    incomplete = Context(media_info=_album_info(total_tracks=3))
    unknown = Context(media_info=_album_info(total_tracks=None))

    assert "仅包含 1 个独立音频文件" in (
        DownloadChain._validate_music_album_resource(incomplete, ["叶惠美/disc.flac"]) or ""
    )
    assert incomplete.confirmed_full_coverage is False
    assert "总曲目数未知" in (
        DownloadChain._validate_music_album_resource(unknown, ["叶惠美/01.flac"]) or ""
    )
    assert "未提供文件清单" in (
        DownloadChain._validate_music_album_resource(
            Context(media_info=_album_info(total_tracks=3)),
            [],
        ) or ""
    )


def test_album_resource_dedupes_same_track_in_different_formats():
    """同一盘同一曲序的多种编码不能冒充多首独立曲目。"""
    context = Context(media_info=_album_info(total_tracks=2))

    error = DownloadChain._validate_music_album_resource(
        context,
        ["叶惠美/01 - 以父之名.flac", "叶惠美/01 - 以父之名.mp3"],
    )

    assert "仅包含 1 个独立音频文件" in (error or "")
    assert context.confirmed_full_coverage is False


def test_download_single_stops_before_client_when_album_pack_is_incomplete():
    """下载入口应在添加任务前拒绝不完整专辑，并记录可供后续候选继续尝试的失败原因。"""
    context = Context(
        media_info=_album_info(total_tracks=3),
        meta_info=MetaMusic.from_music_info(_album_info(total_tracks=3)),
        torrent_info=TorrentInfo(
            title="周杰伦 - 叶惠美 FLAC",
            category=MediaType.MUSIC.value,
        ),
    )
    chain = DownloadChain()
    chain._record_download_failure = Mock()
    media_chain = Mock()
    media_chain.supplement_tmdb_info.return_value = context.media_info
    torrent_helper = Mock()
    torrent_helper.get_fileinfo_from_torrent_content.return_value = (
        "叶惠美",
        ["叶惠美/整轨.flac", "叶惠美/整轨.cue"],
    )

    with patch("app.application.orchestration.download.MediaChain", return_value=media_chain), \
            patch("app.application.orchestration.download.TorrentHelper", return_value=torrent_helper), \
            patch("app.application.orchestration.download.eventmanager.send_event", return_value=None):
        task_id, error = chain.download_single(
            context,
            torrent_content=b"torrent",
            return_detail=True,
        )

    assert task_id is None
    assert "专辑资源不完整" in error
    chain._record_download_failure.assert_called_once()


def test_download_endpoint_builds_music_context():
    """现有添加下载接口应使用 MusicInfo 和 MetaMusic 构造音乐上下文。"""
    chain = Mock()
    chain.download_single.return_value = "hash-1"
    current_user = Mock(name="admin")

    with patch("app.api.endpoints.download.DownloadChain", return_value=chain):
        response = download(
            media_in=MusicInfoSchema(**_music_info().to_dict()),
            torrent_in=TorrentInfo(
                title="周杰伦 - 叶惠美 FLAC",
                enclosure="https://example.com/download?id=1",
                category="音乐",
            ),
            downloader="qb",
            save_path=None,
            current_user=current_user,
        )

    assert response.success is True
    context = chain.download_single.call_args.kwargs["context"]
    assert isinstance(context.media_info, MusicInfo)
    assert context.media_info.media_id == "recording-1"
    assert context.meta_info.type == MediaType.MUSIC
    assert context.meta_info.org_string == "周杰伦 - 叶惠美 FLAC"


def test_download_add_forwards_album_namespace_to_media_chain():
    """无完整媒体上下文的专辑下载必须在精确识别前保留 album 命名空间。"""
    media_chain = Mock()
    media_chain.recognize_media.return_value = _album_info(total_tracks=11)
    download_chain = Mock()
    download_chain.download_single.return_value = "hash-album"

    with patch("app.api.endpoints.download.MediaChain", return_value=media_chain), patch(
        "app.api.endpoints.download.DownloadChain", return_value=download_chain
    ):
        response = add(
            torrent_in=TorrentInfo(
                title="周杰伦 - 叶惠美 FLAC",
                enclosure="https://example.com/album.torrent",
                category=MediaType.MUSIC.value,
            ),
            media_source="musicbrainz",
            media_id="release-group-1",
            music_type="album",
            current_user=Mock(name="admin"),
        )

    assert response.success is True
    recognize_kwargs = media_chain.recognize_media.call_args.kwargs
    assert isinstance(recognize_kwargs["meta"], MetaMusic)
    assert recognize_kwargs["mtype"] == MediaType.MUSIC
    assert recognize_kwargs["music_type"] == MUSIC_ENTITY_ALBUM
    context = download_chain.download_single.call_args.kwargs["context"]
    assert context.media_info.music_type == MUSIC_ENTITY_ALBUM


def test_music_library_exists_uses_atomic_album_lookup():
    """整专存在性检查应按音乐条目判断，不能落入电视剧季集补全分支。"""
    album = _album_info(total_tracks=11)
    chain = DownloadChain()
    chain.media_exists = Mock(
        return_value=ExistMediaInfo(
            type=MediaType.MUSIC,
            server_type="navidrome",
            server="music",
            itemid="album-item-1",
        )
    )
    mediaserver = Mock()
    mediaserver.get_item_id.return_value = "album-item-1"

    with patch("app.application.orchestration.download.MediaServerOper", return_value=mediaserver):
        exists, no_exists = chain.get_no_exists_info(
            meta=MetaMusic.from_music_info(album),
            mediainfo=album,
        )

    assert exists is True
    assert no_exists == {}
    mediaserver.get_item_id.assert_called_once_with(
        mtype=MediaType.MUSIC.value,
        title="叶惠美",
        year=None,
    )
    chain.media_exists.assert_called_once_with(
        mediainfo=album,
        itemid="album-item-1",
    )
