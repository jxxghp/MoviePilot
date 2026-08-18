from unittest.mock import patch

from app.application.orchestration.transfer import TransferChain
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.schemas import TransferInfo
from app.schemas.tmdb import TmdbEpisode
from app.schemas.types import ContentType, MediaType, MessageType


def test_send_transfer_message_passes_episode_info_to_template_context() -> None:
    """
    入库成功通知应把当前季集信息传给消息模板，确保 total_episodes 可渲染。
    """
    chain = TransferChain()
    meta = MetaBase("Test.Show.S01E01.mkv")
    meta.type = MediaType.TV
    meta.name = "Test Show"
    meta.begin_season = 1
    meta.begin_episode = 1
    episodes_info = [
        TmdbEpisode(episode_number=1, name="第一集"),
        TmdbEpisode(episode_number=2, name="第二集"),
    ]
    mediainfo = MediaInfo(
        type=MediaType.TV,
        title="Test Show",
        season=1,
        tmdb_id=12345,
    )
    transferinfo = TransferInfo(success=True)

    with patch.object(chain, "post_message") as post_message:
        chain.send_transfer_message(
            meta=meta,
            mediainfo=mediainfo,
            transferinfo=transferinfo,
            season_episode="S01 E01",
            episodes_info=episodes_info,
            username="tester",
        )

    message = post_message.call_args.args[0]
    assert message.mtype == MessageType.Organize
    assert message.ctype == ContentType.OrganizeSuccess
    assert post_message.call_args.kwargs["episodes_info"] is episodes_info
    assert post_message.call_args.kwargs["season_episode"] == "S01 E01"
