import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.core.config import ConfigModel
from app.modules.acoustid import AcoustIdModule
from app.utils.http import AsyncRequestUtils, RequestUtils


RECORDING_ID = "38035858-f990-4fbb-b3b2-f2f8b958eeba"


class FakeResponse:
    """提供 AcoustID 模块测试所需的最小 HTTP 响应接口。"""

    def __init__(self, payload: dict, status_code: int = 200) -> None:
        """保存响应数据并记录资源是否被关闭。"""
        self.payload = payload
        self.status_code = status_code
        self.closed = False

    def json(self) -> dict:
        """返回预设 JSON 响应。"""
        return self.payload

    def close(self) -> None:
        """记录响应资源已释放。"""
        self.closed = True


class FakeAsyncResponse(FakeResponse):
    """提供异步 AcoustID 查询所需的响应关闭接口。"""

    async def aclose(self) -> None:
        """记录异步响应资源已释放。"""
        self.closed = True


def test_acoustid_api_key_has_built_in_default():
    """系统应内置可用 AcoustID 应用 Key，同时允许运行配置覆盖。"""
    assert ConfigModel.model_fields["ACOUSTID_API_KEY"].default == "b1auxfOzAg"


def test_identify_music_by_fingerprint_queries_acoustid_and_caches_result(
        tmp_path,
        monkeypatch,
):
    """有效指纹应请求 recordingids，并按文件状态缓存 MusicBrainz ID。"""
    audio_path = tmp_path / "track.flac"
    audio_path.write_bytes(b"audio")
    module = AcoustIdModule()
    module._fpcalc_path = "/usr/bin/fpcalc"
    fpcalc = Mock(return_value=SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"duration": 243.4, "fingerprint": "AQADtM..."}),
    ))
    response = FakeResponse({
        "status": "ok",
        "results": [{
            "score": 0.98,
            "recordings": [{"id": RECORDING_ID}],
        }],
    })
    post_res = Mock(return_value=response)
    monkeypatch.setattr("app.modules.acoustid.subprocess.run", fpcalc)
    monkeypatch.setattr(RequestUtils, "post_res", post_res)
    monkeypatch.setattr(module, "_wait_for_rate_limit", lambda: None)
    monkeypatch.setattr("app.modules.acoustid.settings.ACOUSTID_API_KEY", "client-key")

    first = module.identify_music_by_fingerprint(audio_path)
    second = module.identify_music_by_fingerprint(audio_path)

    assert first == RECORDING_ID
    assert second == RECORDING_ID
    fpcalc.assert_called_once_with(
        ["/usr/bin/fpcalc", "-json", str(audio_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    post_res.assert_called_once()
    assert post_res.call_args.kwargs["url"] == "https://api.acoustid.org/v2/lookup"
    assert post_res.call_args.kwargs["data"] == {
        "client": "client-key",
        "duration": 243,
        "fingerprint": "AQADtM...",
        "meta": "recordingids",
        "format": "json",
    }
    assert response.closed is True


def test_select_recording_id_requires_high_score_and_valid_uuid():
    """低置信结果和异常外部 ID 不得进入 MusicBrainz 详情查询。"""
    payload = {
        "status": "ok",
        "results": [
            {"score": 0.99, "recordings": [{"id": "not-a-uuid"}]},
            {"score": 0.91, "recordings": [{"id": RECORDING_ID}]},
            {
                "score": 0.89,
                "recordings": [{"id": "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d"}],
            },
        ],
    }

    assert AcoustIdModule._select_recording_id(payload) == RECORDING_ID
    assert AcoustIdModule._select_recording_id({
        "status": "ok",
        "results": [{
            "score": 0.89,
            "recordings": [{"id": RECORDING_ID}],
        }],
    }) is None


def test_identify_music_by_fingerprint_skips_missing_fpcalc(tmp_path, monkeypatch):
    """缺少 fpcalc 时应静默跳过指纹层，不发起 AcoustID 请求。"""
    audio_path = tmp_path / "track.mp3"
    audio_path.write_bytes(b"audio")
    module = AcoustIdModule()
    post_res = Mock()
    monkeypatch.setattr(RequestUtils, "post_res", post_res)

    assert module.identify_music_by_fingerprint(Path(audio_path)) is None
    post_res.assert_not_called()


def test_async_identify_music_by_fingerprint_uses_async_process_and_http(
        tmp_path,
        monkeypatch,
):
    """异步接口应异步执行 fpcalc 与 HTTP 查询，并复用同一响应筛选规则。"""
    audio_path = tmp_path / "track.flac"
    audio_path.write_bytes(b"audio")
    module = AcoustIdModule()
    module._fpcalc_path = "/usr/bin/fpcalc"

    class FakeProcess:
        """模拟已成功执行的异步 fpcalc 子进程。"""

        returncode = 0

        async def communicate(self):
            """返回 fpcalc JSON 标准输出和空错误输出。"""
            payload = json.dumps({
                "duration": 243.4,
                "fingerprint": "AQADtM...",
            })
            return payload.encode(), b""

    create_process = AsyncMock(return_value=FakeProcess())
    response = FakeAsyncResponse({
        "status": "ok",
        "results": [{
            "score": 0.98,
            "recordings": [{"id": RECORDING_ID}],
        }],
    })
    post_res = AsyncMock(return_value=response)
    wait_rate_limit = AsyncMock()
    monkeypatch.setattr(
        "app.modules.acoustid.asyncio.create_subprocess_exec",
        create_process,
    )
    monkeypatch.setattr(AsyncRequestUtils, "post_res", post_res)
    monkeypatch.setattr(module, "_async_wait_for_rate_limit", wait_rate_limit)
    monkeypatch.setattr("app.modules.acoustid.settings.ACOUSTID_API_KEY", "client-key")

    result = asyncio.run(module.async_identify_music_by_fingerprint(audio_path))

    assert result == RECORDING_ID
    create_process.assert_awaited_once_with(
        "/usr/bin/fpcalc",
        "-json",
        str(audio_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    wait_rate_limit.assert_awaited_once()
    post_res.assert_awaited_once()
    assert post_res.await_args.kwargs["data"]["meta"] == "recordingids"
    assert response.closed is True
