from unittest.mock import Mock, patch

from app.api.endpoints.download import download
from app.chain.download import DownloadChain
from app.chain.music import MusicChain
from app.core.music import MusicInfo
from app.schemas.context import TorrentInfo
from app.schemas.music import MusicInfo as MusicInfoSchema
from app.schemas.types import MediaType


def _music_info() -> MusicInfo:
    """构造下载测试使用的标准音乐信息。"""
    return MusicInfo(
        source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        year=2003,
        cover_url="https://example.com/cover.jpg",
        raw_data={"large": "payload"},
    )


def test_music_info_exposes_download_chain_compatibility_fields():
    """音乐信息应安全兼容下载链现有的视频身份字段访问。"""
    info = _music_info()
    meta = MusicChain.to_meta(info)

    assert info.type == MediaType.MUSIC
    assert info.tmdb_id is None
    assert info.episode_group is None
    assert meta.episode_list == []
    assert meta.season_episode == ""


def test_download_note_keeps_versioned_music_context():
    """音乐下载历史备注应保存可恢复且不含上游原始大对象的上下文。"""
    info = _music_info()
    meta = MusicChain.to_meta(info)

    note = DownloadChain._build_download_note("Manual", info, meta)

    assert note["source"] == "Manual"
    assert note["music"]["version"] == 1
    assert note["music"]["meta"]["album"] == "叶惠美"
    assert note["music"]["media"]["media_id"] == "recording-1"
    assert "raw_data" not in note["music"]["media"]


def test_download_endpoint_builds_music_context():
    """现有添加下载接口应使用 MusicInfo 和 MusicMeta 构造音乐上下文。"""
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
