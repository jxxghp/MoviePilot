"""
验证整理重新识别时下传已核实的来源原生标识。

智能助手核实媒体身份与季集后，重新规划会写入 media_source/media_id 并清空旧识别结果。
若重新识别只按标题搜索，拼音文件名或错名文件会再次识别失败，重新规划等于白做。
"""

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.api.arguments import canonical_api_arguments
from app.agent.tools.impl.api import MoviePilotApiTool
from app.chain.media import MediaChain
from app.chain.media import plugin as media_plugin
from app.chain.transfer import execution as transfer_execution
from app.chain.transfer.execution import TransferExecutionOwner
from app.domain.context import MediaInfo
from app.domain.meta.metavideo import MetaVideo
from app.schemas.types import MediaSource, MediaType


def _capture_recognition(monkeypatch) -> dict:
    """
    替换媒体链，记录重新识别实际收到的参数。
    """
    captured = {}

    class _FakeMediaChain:
        """
        记录识别调用的伪媒体链。
        """

        def recognize_by_meta(self, meta, **options):
            """
            保存元数据与识别选项，不返回媒体信息。
            """
            captured["meta"] = meta
            captured["options"] = options
            return None

    monkeypatch.setattr(transfer_execution, "MediaChain", _FakeMediaChain)
    return captured


def _task(**overrides) -> SimpleNamespace:
    """
    构造只带识别所需字段的整理任务。
    """
    task = SimpleNamespace(
        meta=SimpleNamespace(name="yongzhe de mingzi S02E03"),
        media_source=MediaSource.TMDB,
        media_id="117061",
        mtype=MediaType.TV,
        download_history=None,
        preview=True,
    )
    task.__dict__.update(overrides)
    return task


def test_replan_recognition_passes_verified_media_id(monkeypatch) -> None:
    """
    已核实的来源与原生标识必须成对下传，重新识别才能按身份而不是按标题进行。
    """
    captured = _capture_recognition(monkeypatch)

    TransferExecutionOwner._recognize_task_meta(_task())

    assert captured["options"]["media_source"] is MediaSource.TMDB
    assert captured["options"]["media_id"] == "117061"
    assert captured["options"]["mtype"] is MediaType.TV


def test_recognition_without_source_does_not_send_bare_media_id(monkeypatch) -> None:
    """
    来源缺失时不能下传孤立标识，否则识别会按成对校验直接失败。
    """
    captured = _capture_recognition(monkeypatch)

    TransferExecutionOwner._recognize_task_meta(_task(media_source=None))

    assert "media_id" not in captured["options"]


def test_recognition_without_media_id_keeps_title_search(monkeypatch) -> None:
    """
    未核实原生标识时保持按标题识别的既有行为。
    """
    captured = _capture_recognition(monkeypatch)

    TransferExecutionOwner._recognize_task_meta(_task(media_id=None))

    assert "media_id" not in captured["options"]
    assert captured["options"]["media_source"] is MediaSource.TMDB


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("plugin_first", [False, True], ids=["native-first", "plugin-first"])
@pytest.mark.parametrize("outcome", ["matched", "missing", "other-source", "other-id", "other-type"])
def test_verified_identity_controls_recognition(
    monkeypatch, asynchronous: bool, plugin_first: bool, outcome: str,
) -> None:
    """
    显式身份在两种识别顺序下都直查，查无结果或身份及类型不符时拒绝继续整理。
    """
    meta = MetaVideo("yongzhe de mingzi S02E03")
    expected = MediaInfo(
        media_source=MediaSource.Douban if outcome == "other-source" else MediaSource.TMDB,
        media_id="999999" if outcome == "other-id" else "117061",
        type=MediaType.MOVIE if outcome == "other-type" else MediaType.TV,
        title="已核实剧集",
    )
    candidate = None if outcome == "missing" else expected
    chain = MediaChain()
    native = AsyncMock(return_value=candidate) if asynchronous else Mock(return_value=candidate)
    plugin = AsyncMock(return_value=expected) if asynchronous else Mock(return_value=expected)
    native_name = "async_recognize_media" if asynchronous else "recognize_media"
    plugin_name = "async_recognize_help" if asynchronous else "recognize_help"
    monkeypatch.setattr(chain, native_name, native)
    monkeypatch.setattr(chain, plugin_name, plugin)
    monkeypatch.setattr(chain.eventmanager, "check", Mock(return_value=True))
    monkeypatch.setattr(media_plugin, "get_chain_runtime_config_snapshot", Mock(
        return_value=SimpleNamespace(recognize_plugin_first=plugin_first),
    ))
    images = AsyncMock() if asynchronous else Mock()
    monkeypatch.setattr(chain, "async_obtain_images" if asynchronous else "obtain_images", images)
    finalizer = Mock(return_value=candidate)
    monkeypatch.setattr(chain, "_finalize_recognition_result", finalizer)
    options = dict(media_source=MediaSource.TMDB, media_id="117061", mtype=MediaType.TV, obtain_images=True)

    if asynchronous:
        result = asyncio.run(chain.async_recognize_by_meta(meta, **options))
    else:
        result = chain.recognize_by_meta(meta, **options)

    assert native.call_count == 1
    assert native.call_args.kwargs["media_source"] is MediaSource.TMDB
    assert native.call_args.kwargs["media_id"] == "117061"
    assert native.call_args.kwargs["mtype"] is MediaType.TV
    assert meta.begin_season == 2 and meta.begin_episode == 3
    plugin.assert_not_called()
    if outcome == "matched":
        assert result is expected
        assert images.call_count == 1
    else:
        assert result is None
        finalizer.assert_not_called()
        images.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_recognition_keeps_existing_positional_arguments(monkeypatch, asynchronous: bool) -> None:
    """
    旧调用中的剧集组、图片选项、媒体类型和音乐类型保留原有位置含义。
    """
    chain = MediaChain()
    fallback = AsyncMock(return_value=None) if asynchronous else Mock(return_value=None)
    method = "_async_recognize_with_fallback_by_meta" if asynchronous else "_recognize_with_fallback_by_meta"
    monkeypatch.setattr(chain, method, fallback)
    meta = MetaVideo("已核实剧集 S02E03")
    args = (meta, MediaSource.TMDB, "episode-group", True, MediaType.TV, None)

    if asynchronous:
        asyncio.run(chain.async_recognize_by_meta(*args))
    else:
        chain.recognize_by_meta(*args)

    assert fallback.call_args.kwargs == dict(
        metainfo=meta, media_source=MediaSource.TMDB, media_id=None,
        episode_group="episode-group", obtain_images=True, mtype=MediaType.TV, music_type=None,
    )


def test_replan_skill_example_matches_runtime_input_contract() -> None:
    """
    失败重试技能的结构化示例必须符合实际 MCP Schema，并保留已核实身份与类型。
    """
    root = Path(__file__).resolve().parents[1]
    skill = (root / "skills/transfer-failed-retry/SKILL.md").read_text(encoding="utf-8")
    examples = re.findall(r"```json\n(.*?)\n```", skill, re.DOTALL)
    assert len(examples) == 1
    payload = json.loads(examples[0])
    schema = MoviePilotApiTool(session_id="replan-contract", user_id="1").get_mcp_input_schema()
    assert canonical_api_arguments(payload, schema) == {**payload, "query": {}}
    model = schema["$defs"]["TransferHistoryReplanRequest"]
    assert set(model["required"]) == {"task_id", "settlement_revision", "media_source", "media_id", "media_type"}
    assert "直接" in model["properties"]["media_id"]["description"]
    assert payload["body"]["media_type"] == "tv"
    assert payload["body"]["season"] == 2 and payload["body"]["episode"] == 3


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_bare_identity_cannot_fall_back_to_plugin(monkeypatch, asynchronous: bool) -> None:
    """
    缺少来源的显式标识由原生入口拒绝，插件优先也不能将其改成标题识别。
    """
    chain = MediaChain()
    plugin = AsyncMock() if asynchronous else Mock()
    runner = AsyncMock() if asynchronous else Mock()
    monkeypatch.setattr(chain, "async_recognize_help" if asynchronous else "recognize_help", plugin)
    monkeypatch.setattr(chain, "_async_run_recognition_steps" if asynchronous else "_run_recognition_steps", runner)
    monkeypatch.setattr(chain.eventmanager, "check", Mock(return_value=True))
    monkeypatch.setattr(media_plugin, "get_chain_runtime_config_snapshot", Mock(
        return_value=SimpleNamespace(recognize_plugin_first=True),
    ))
    meta = MetaVideo("yongzhe de mingzi S02E03")

    if asynchronous:
        result = asyncio.run(chain.async_recognize_by_meta(meta, media_id="117061"))
    else:
        result = chain.recognize_by_meta(meta, media_id="117061")

    assert result is None
    runner.assert_not_called()
    plugin.assert_not_called()


def test_replan_generated_schema_and_skill_stay_in_sync() -> None:
    """
    重新生成后仍保留重规划输入的直查约束，发布的 Schema 与技能内容使用同一定义。
    """
    from scripts.generate_agent_api_mcp_schema import generate_schema
    from scripts.generate_agent_skill_docs import _render_api_docs

    root = Path(__file__).resolve().parents[1]
    stored_schema = json.loads((root / "app/agent/policy/resources/api_mcp_schema.json").read_text(encoding="utf-8"))
    assert stored_schema["$defs"]["TransferHistoryReplanRequest"] == generate_schema()["$defs"]["TransferHistoryReplanRequest"]
    heading = "### `transfer.history.replan`"
    generated_operation = heading + _render_api_docs().split(heading, 1)[1].split("\n### `", 1)[0]
    skill = (root / "skills/moviepilot-api/api/transfer.md").read_text(encoding="utf-8")
    assert generated_operation.strip() in skill
