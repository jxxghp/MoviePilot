import json
from types import SimpleNamespace

from app.chain.subscribe import SubscribeChain
from app.schemas.types import MediaType


def test_subscribe_source_keyword_includes_episode_group():
    subscribe = SimpleNamespace(
        id=1,
        name="Test Show",
        year="2026",
        type=MediaType.TV.value,
        season=1,
        episode_group="group-season-03",
        tmdbid=12345,
        imdbid=None,
        tvdbid=None,
        doubanid=None,
        bangumiid=None,
    )

    source = SubscribeChain.get_subscribe_source_keyword(subscribe)
    payload = json.loads(source.split("|", 1)[1])

    assert payload["episode_group"] == "group-season-03"
    assert SubscribeChain.parse_subscribe_source_keyword(source)["episode_group"] == "group-season-03"
