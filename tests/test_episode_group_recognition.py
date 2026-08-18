import sys
from types import ModuleType
from unittest.mock import patch

sys.modules.setdefault("qbittorrentapi", ModuleType("qbittorrentapi"))
setattr(sys.modules["qbittorrentapi"], "TorrentFilesList", list)
sys.modules.setdefault("transmission_rpc", ModuleType("transmission_rpc"))
setattr(sys.modules["transmission_rpc"], "File", object)
sys.modules.setdefault("psutil", ModuleType("psutil"))

from app.application.orchestration import ChainBase
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.types import MediaSource, MediaType


def test_recognize_media_uses_meta_episode_group():
    """
    识别链未显式传 episode_group 时，应沿用元数据中识别出的剧集组。
    """
    group_id = "5ad0ec240e0a26303f00d84d"
    chain = ChainBase()
    meta = MetaBase("测试剧集")
    meta.name = "测试剧集"
    meta.type = MediaType.TV
    meta.episode_group = group_id
    mediainfo = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="100",
        title="测试剧集",
        year="2024",
        tmdb_id=100,
        type=MediaType.TV,
    )

    with patch.object(chain, "unicast", return_value=mediainfo) as unicast, patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.report_recognize_share",
        return_value=True,
    ), patch("app.application.orchestration._recognition.MoviePilotServerHelper.query_recognize_share") as query_mock:
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is mediainfo
    assert unicast.call_args.kwargs["episode_group"] == group_id
    query_mock.assert_not_called()
