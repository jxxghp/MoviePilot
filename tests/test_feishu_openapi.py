"""飞书 OpenAPI 客户端测试：令牌缓存与失效重试、请求路径与请求体、上传下载和失败归一。"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from app.modules.feishu import openapi
from app.modules.feishu.openapi import NETWORK_ERROR_CODE, FeishuOpenApi, _parse_file_name


class FakeResponse:
    """模拟 requests.Response 中客户端用到的属性。"""

    def __init__(self, body: Any = None, status_code: int = 200, headers: Optional[Dict[str, str]] = None,
                 content: bytes = b""):
        self._body = body
        self.status_code = status_code
        self.reason = "OK" if status_code == 200 else "Bad Request"
        self.headers = headers or {}
        self.content = content

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeHttp:
    """替代 RequestUtils：记录每次请求，按队列返回预设响应。"""

    def __init__(self, token_responses: List[FakeResponse], responses: List[Optional[FakeResponse]]):
        self.token_responses = list(token_responses)
        self.responses = list(responses)
        self.token_calls: List[Dict[str, Any]] = []
        self.calls: List[Dict[str, Any]] = []

    def post_res(self, url, **kwargs):
        self.token_calls.append({"url": url, **kwargs})
        return self.token_responses.pop(0)

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)

    def close(self):
        pass


def _token(value: str = "t-1", expire: int = 7200) -> FakeResponse:
    return FakeResponse({"code": 0, "msg": "ok", "tenant_access_token": value, "expire": expire})


def _client(http: FakeHttp) -> FeishuOpenApi:
    client = FeishuOpenApi("cli_app", "secret")
    client._http = http
    return client


def _ok(data: Optional[dict] = None, log_id: str = "log-1") -> FakeResponse:
    return FakeResponse({"code": 0, "msg": "success", "data": data or {}}, headers={"X-Tt-Logid": log_id})


def test_create_message_request_and_cached_token():
    """发送消息使用 IM 接口的路径、查询参数与请求体，令牌只获取一次并复用。"""
    http = FakeHttp([_token()], [_ok({"message_id": "om_1"}), _ok({"message_id": "om_2"})])
    client = _client(http)

    first = client.create_message("open_id", "ou_1", "text", '{"text": "hi"}', "uuid-1")
    client.create_message("chat_id", "oc_1", "text", '{"text": "again"}', "uuid-2")

    assert first.success()
    assert first.data == {"message_id": "om_1"}
    assert first.log_id == "log-1"
    assert http.token_calls == [{
        "url": "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        "json": {"app_id": "cli_app", "app_secret": "secret"},
    }]
    call = http.calls[0]
    assert (call["method"], call["url"]) == ("POST", "https://open.feishu.cn/open-apis/im/v1/messages")
    assert call["params"] == {"receive_id_type": "open_id"}
    assert call["json"] == {"receive_id": "ou_1", "msg_type": "text", "content": '{"text": "hi"}', "uuid": "uuid-1"}
    assert call["headers"]["Authorization"] == "Bearer t-1"
    assert http.calls[1]["headers"]["Authorization"] == "Bearer t-1"


def test_token_refreshes_ten_minutes_before_expiry(monkeypatch):
    """令牌在过期前 10 分钟即重新获取。"""
    now = [1000.0]
    monkeypatch.setattr(openapi.time, "time", lambda: now[0])
    http = FakeHttp([_token("t-1", expire=7200), _token("t-2")], [_ok(), _ok()])
    client = _client(http)

    client.patch_message("om_1", "{}")
    now[0] += 7200 - 10 * 60 + 1
    client.patch_message("om_1", "{}")

    assert [c["headers"]["Authorization"] for c in http.calls] == ["Bearer t-1", "Bearer t-2"]


def test_invalid_token_is_refreshed_and_retried_once():
    """接口判定令牌失效时刷新令牌并重试一次，不会无限重试。"""
    invalid = FakeResponse({"code": 99991663, "msg": "Invalid access token"}, status_code=400)
    http = FakeHttp([_token("stale"), _token("fresh")], [invalid, _ok({"reaction_id": "r1"})])
    client = _client(http)

    response = client.create_message_reaction("om_1", "GLANCE")

    assert response.data == {"reaction_id": "r1"}
    assert [c["headers"]["Authorization"] for c in http.calls] == ["Bearer stale", "Bearer fresh"]
    assert http.calls[1]["json"] == {"reaction_type": {"emoji_type": "GLANCE"}}


def test_token_failure_skips_request():
    """获取令牌失败时不发出业务请求，并返回本地错误码。"""
    http = FakeHttp([FakeResponse({"code": 10003, "msg": "invalid app_secret"})], [])
    client = _client(http)

    response = client.create_card("{}")

    assert not response.success()
    assert response.code == NETWORK_ERROR_CODE
    assert http.calls == []


def test_business_error_keeps_code_and_log_id():
    """开放平台业务错误原样保留 code/msg/log_id，便于日志排查。"""
    error = FakeResponse({"code": 230002, "msg": "bot not in chat"}, status_code=400, headers={"X-Tt-Logid": "L9"})
    http = FakeHttp([_token()], [error])

    response = _client(http).reply_message("om_1", "text", "{}", False, "u")

    assert (response.code, response.msg, response.log_id, response.data) == (230002, "bot not in chat", "L9", {})


@pytest.mark.parametrize("response", [None, FakeResponse(None, status_code=502)])
def test_network_failure_and_non_json_are_normalized(response):
    """网络失败或非 JSON 响应统一为失败响应，不抛异常。"""
    http = FakeHttp([_token()], [response])

    result = _client(http).patch_message("om_1", "{}")

    assert not result.success()
    assert result.code == NETWORK_ERROR_CODE


def test_path_params_are_url_encoded():
    """路径参数整体编码，含 / 或 ? 的 ID 不会改写请求目标。"""
    http = FakeHttp([_token()], [_ok()])

    _client(http).delete_message_reaction("om/1?x", "r#1")

    assert http.calls[0]["method"] == "DELETE"
    assert http.calls[0]["url"] == "https://open.feishu.cn/open-apis/im/v1/messages/om%2F1%3Fx/reactions/r%231"


def test_cardkit_requests():
    """CardKit 创建、流式更新与设置更新使用各自的方法、路径和请求体。"""
    http = FakeHttp([_token()], [_ok({"card_id": "c1"}), _ok(), _ok()])
    client = _client(http)

    assert client.create_card('{"schema": "2.0"}').data == {"card_id": "c1"}
    client.update_card_element_content("c1", "body", "文本", 2, "u2")
    client.update_card_settings("c1", '{"config": {}}', 3, "u3")

    create, content, settings = http.calls
    assert (create["method"], create["url"]) == ("POST", "https://open.feishu.cn/open-apis/cardkit/v1/cards")
    assert create["json"] == {"type": "card_json", "data": '{"schema": "2.0"}'}
    assert content["method"] == "PUT"
    assert content["url"].endswith("/open-apis/cardkit/v1/cards/c1/elements/body/content")
    assert content["json"] == {"uuid": "u2", "content": "文本", "sequence": 2}
    assert settings["method"] == "PATCH"
    assert settings["url"].endswith("/open-apis/cardkit/v1/cards/c1/settings")
    assert settings["json"] == {"settings": '{"config": {}}', "uuid": "u3", "sequence": 3}


def test_upload_image_and_file_use_multipart(tmp_path: Path):
    """图片与文件以 multipart 上传，表单字段与文件内容正确。"""
    image = tmp_path / "poster.png"
    image.write_bytes(b"\x89PNG")
    audio = tmp_path / "voice.opus"
    audio.write_bytes(b"OggS")
    http = FakeHttp([_token()], [_ok({"image_key": "img_1"}), _ok({"file_key": "file_1"})])
    client = _client(http)

    assert client.upload_image(image).data == {"image_key": "img_1"}
    assert client.upload_file(audio, "opus", "语音.opus", duration=1500).data == {"file_key": "file_1"}

    image_call, file_call = http.calls
    assert image_call["url"].endswith("/open-apis/im/v1/images")
    assert image_call["data"] == {"image_type": "message"}
    assert image_call["files"] == {"image": ("poster.png", b"\x89PNG")}
    assert file_call["data"] == {"file_type": "opus", "file_name": "语音.opus", "duration": "1500"}
    assert file_call["files"] == {"file": ("语音.opus", b"OggS")}
    assert "json" not in image_call


def test_download_message_resource_returns_bytes_name_and_type():
    """下载资源返回内容、Content-Disposition 文件名和 Content-Type。"""
    download = FakeResponse(
        status_code=200,
        headers={
            "Content-Type": "application/pdf",
            "Content-Disposition": "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.pdf",
        },
        content=b"%PDF",
    )
    http = FakeHttp([_token()], [download])

    result = _client(http).download_message_resource("om_1", "file_v2", "file")

    assert result == (b"%PDF", "报告.pdf", "application/pdf")
    assert http.calls[0]["url"].endswith("/open-apis/im/v1/messages/om_1/resources/file_v2")
    assert http.calls[0]["params"] == {"type": "file"}


def test_download_failure_returns_none():
    """下载接口返回错误 JSON 时不把错误体当作文件内容。"""
    http = FakeHttp([_token()], [FakeResponse({"code": 234003, "msg": "file not found"}, status_code=400)])

    assert _client(http).download_image("img_1") is None


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ('attachment; filename="poster.jpg"', "poster.jpg"),
        ("attachment; filename=%E5%9B%BE.png", "图.png"),
        # requests 以 latin-1 解码响应头时，UTF-8 原文需要还原。
        ('attachment; filename="' + "报告.pdf".encode("utf-8").decode("latin-1") + '"', "报告.pdf"),
    ],
)
def test_parse_file_name(header, expected):
    assert _parse_file_name(header) == expected


def test_response_bodies_are_json_strings_not_reencoded():
    """content 等字段按调用方给出的 JSON 字符串原样发送，不做二次编码。"""
    http = FakeHttp([_token()], [_ok()])
    content = json.dumps({"text": "你好"}, ensure_ascii=False)

    _client(http).reply_message("om_1", "text", content, True, "u1")

    assert http.calls[0]["json"]["content"] == content
    assert http.calls[0]["json"]["reply_in_thread"] is True


def test_upload_retry_after_invalid_token_resends_full_content(tmp_path: Path):
    """令牌失效重试时重新上传完整文件内容，而不是已读到末尾的空句柄。"""
    image = tmp_path / "poster.png"
    image.write_bytes(b"\x89PNG-full")
    invalid = FakeResponse({"code": 99991663, "msg": "Invalid access token"}, status_code=400)
    http = FakeHttp([_token("stale"), _token("fresh")], [invalid, _ok({"image_key": "img_1"})])

    assert _client(http).upload_image(image).data == {"image_key": "img_1"}
    assert [c["files"] for c in http.calls] == [{"image": ("poster.png", b"\x89PNG-full")}] * 2
