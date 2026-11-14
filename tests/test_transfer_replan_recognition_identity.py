"""
验证整理重新识别时下传已核实的来源原生标识。

智能助手核实媒体身份与季集后，重新规划会写入 media_source/media_id 并清空旧识别结果。
若重新识别只按标题搜索，拼音文件名或错名文件会再次识别失败，重新规划等于白做。
"""

from types import SimpleNamespace

from app.chain.transfer import execution as transfer_execution
from app.chain.transfer.execution import TransferExecutionOwner
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
