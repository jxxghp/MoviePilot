from app.domain.context import MusicInfo
from app.modules.musixmatch import MusixmatchModule


def _payload(name: str, item: dict) -> dict:
    """构造 Musixmatch 官方 message/body 响应包装。"""
    return {
        "message": {
            "header": {"status_code": 200},
            "body": {name: item},
        }
    }


def test_musixmatch_prefers_authorized_subtitle(monkeypatch) -> None:
    """官方 matcher 返回可用字幕时不应再请求纯文本歌词。"""
    module = MusixmatchModule()
    calls = []

    def request(method, params):
        calls.append((method, params))
        return _payload("subtitle", {
            "subtitle_id": 12,
            "subtitle_body": "[00:01.00]Track",
            "subtitle_language": "en",
            "restricted": 0,
        })

    monkeypatch.setattr(module, "_request", request)
    results = module.music_lyrics_candidates(
        MusicInfo(title="Track", artists=["Artist"], duration=180)
    )

    assert len(results) == 1
    assert results[0].synced_lyrics == "[00:01.00]Track"
    assert calls[0][0] == "matcher.subtitle.get"
    assert calls[0][1]["f_subtitle_length_max_deviation"] == 2
    assert len(calls) == 1


def test_musixmatch_restricted_results_are_not_saved(monkeypatch) -> None:
    """授权计划标记 restricted 的字幕和歌词均不得写入本地。"""
    module = MusixmatchModule()
    responses = iter([
        _payload("subtitle", {"restricted": 1, "subtitle_body": "blocked"}),
        _payload("lyrics", {"restricted": 1, "lyrics_body": "blocked"}),
    ])
    monkeypatch.setattr(module, "_request", lambda *_args, **_kwargs: next(responses))

    assert module.music_lyrics_candidates(
        MusicInfo(title="Track", artists=["Artist"])
    ) == []
