"""系统配置事件发布边界测试。"""

from unittest.mock import AsyncMock, Mock

import pytest

from app.adapters.system import rust as rust_accel
from app.domain import metainfo as metainfo_module
from app.schemas.types import EventType, SystemConfigKey
from app.startup.composition import system as system_composition


@pytest.mark.anyio
async def test_recognition_config_clears_rust_options_before_event_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """识别配置写入返回前必须清缓存，再把异步广播事件加入队列。"""
    order: list[str] = []
    clear_cache = Mock(side_effect=lambda: order.append("clear"))
    send_event = AsyncMock(side_effect=lambda **_kwargs: order.append("event"))
    monkeypatch.setattr(system_composition, "clear_rust_parse_options_cache", clear_cache)
    monkeypatch.setattr(system_composition.eventmanager, "async_send_event", send_event)

    await system_composition._ConfigurationEventAdapter().publish(
        SystemConfigKey.CustomIdentifiers,
        ["旧名 => 新名"],
    )

    assert order == ["clear", "event"]
    clear_cache.assert_called_once_with()
    assert send_event.await_args.kwargs["etype"] is EventType.ConfigChanged
    payload = send_event.await_args.kwargs["data"]
    assert payload.key == {SystemConfigKey.CustomIdentifiers.value}


@pytest.mark.anyio
async def test_unrelated_config_does_not_clear_rust_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无关设置仍发布配置事件，但不得重建 Rust 识别选项。"""
    clear_cache = Mock()
    send_event = AsyncMock()
    monkeypatch.setattr(system_composition, "clear_rust_parse_options_cache", clear_cache)
    monkeypatch.setattr(system_composition.eventmanager, "async_send_event", send_event)

    await system_composition._ConfigurationEventAdapter().publish("PORT", 3001)

    clear_cache.assert_not_called()
    send_event.assert_awaited_once()


@pytest.mark.anyio
async def test_recognition_config_rebuilds_cached_rust_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """识别词更新事件后，下一次 Rust 解析选项必须读取最新配置。"""
    custom_words = ["旧名 => 旧标题"]
    issue_rule = (
        "HNTV.The.Four(?=.*CMCTV) => "
        "少年四大名捕 S01 {[tmdbid=83905;type=tv;s=1]}"
    )
    monkeypatch.setattr(metainfo_module, "get_custom_words", lambda: list(custom_words))
    monkeypatch.setattr(metainfo_module, "get_media_extensions", lambda: (".mkv",))
    monkeypatch.setattr(metainfo_module, "get_customization", lambda: ())
    monkeypatch.setattr(
        metainfo_module.ReleaseGroupsMatcher,
        "get_release_groups",
        lambda _self: "",
    )
    monkeypatch.setattr(
        "app.domain.meta.streamingplatform.StreamingPlatforms.get_lookup_cache",
        lambda _self: {},
    )
    monkeypatch.setattr(
        system_composition.eventmanager,
        "async_send_event",
        AsyncMock(),
    )
    metainfo_module.clear_rust_parse_options_cache()

    try:
        assert metainfo_module._rust_parse_options()["custom_words"] == ["旧名 => 旧标题"]
        custom_words[:] = [issue_rule]

        await system_composition._ConfigurationEventAdapter().publish(
            SystemConfigKey.CustomIdentifiers.value,
            list(custom_words),
        )

        options = metainfo_module._rust_parse_options()
        assert options["custom_words"] == custom_words
        if not rust_accel.is_available():
            pytest.skip("moviepilot-rust 扩展未安装")
        monkeypatch.setattr(rust_accel, "is_config_enabled", lambda: True)
        result = rust_accel.parse_metainfo(
            "HNTV.The.Four.Complete.720p.HDTV.x264-CMCTV",
            options=options,
        )
        assert result is not None
        assert result["media_source"] == "themoviedb"
        assert result["media_id"] == "83905"
        assert result["begin_season"] == 1
        assert result["apply_words"] == [issue_rule]
    finally:
        metainfo_module.clear_rust_parse_options_cache()
