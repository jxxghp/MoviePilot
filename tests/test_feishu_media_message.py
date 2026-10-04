import json
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from app.testing.bootstrap import ensure_optional_stub

ensure_optional_stub("psutil")
ensure_optional_stub("dateparser")
ensure_optional_stub("Pinyin2Hanzi", is_pinyin=lambda value: False)

from app.domain.context import MediaInfo  # noqa: E402
from app.modules.feishu.feishu import Feishu  # noqa: E402
from app.modules.feishu.openapi import FeishuResponse  # noqa: E402
from app.schemas.message import Message  # noqa: E402

_IMAGE_PROXY = {"http": "http://proxy.example:8080", "https": "http://proxy.example:8080"}


def _build_feishu_client() -> Feishu:
    """构造不会启动真实飞书长连接的测试客户端。"""
    with (
        patch.object(Feishu, "_build_api_client", return_value=MagicMock()),
        patch.object(Feishu, "_start_ws_client"),
    ):
        return Feishu(
            FEISHU_APP_ID="test_app_id",
            FEISHU_APP_SECRET="test_app_secret",
            name="feishu-test",
        )


def test_send_medias_message_passes_first_available_image() -> None:
    """飞书媒体列表应将首张可用媒体图片传入通知卡片。"""
    client = _build_feishu_client()
    first_media = MediaInfo()
    first_media.title = "无海报媒体"
    second_media = MediaInfo()
    second_media.title = "有海报媒体"
    second_media.poster_path = "https://example.com/poster.jpg"

    with patch.object(
        client,
        "send_notification",
        return_value={"success": True},
    ) as send_notification:
        result = client.send_medias_message(
            message=Message(title="搜索结果", userid="ou_test"),
            medias=[first_media, second_media],
        )

    assert result == {"success": True}
    proxy_message = send_notification.call_args.args[0]
    assert proxy_message.image == "https://example.com/poster.jpg"
    assert proxy_message.text == "1. 无海报媒体\n2. 有海报媒体"
    assert send_notification.call_args.kwargs["userid"] == "ou_test"


@pytest.mark.parametrize(
    ("image_url", "proxy", "internal", "expected_referer", "expected_proxy"),
    [
        (
            "https://img9.doubanio.com/view/photo/l/public/poster.webp",
            _IMAGE_PROXY, False, "https://movie.douban.com/", None,
        ),
        (
            "https://img3.doubanio.com/view/photo/l/public/poster.webp",
            None, False, "https://movie.douban.com/", None,
        ),
        ("https://image.tmdb.org/t/p/w500/poster.jpg", _IMAGE_PROXY, False, None, _IMAGE_PROXY),
        ("https://image.tmdb.org/t/p/w500/poster.jpg", None, False, None, None),
        ("http://192.168.1.2:8096/poster", _IMAGE_PROXY, True, None, None),
        ("http://media.local:8096/poster", _IMAGE_PROXY, True, None, None),
    ],
    ids=["douban-proxy", "douban-direct", "tmdb-proxy", "tmdb-direct", "lan-ip", "lan-domain"],
)
def test_send_notification_downloads_image_with_source_policy(
    image_url: str,
    proxy: Optional[dict[str, str]],
    internal: bool,
    expected_referer: Optional[str],
    expected_proxy: Optional[dict[str, str]],
) -> None:
    """海报下载遵守豆瓣防盗链、外网代理和内网直连策略，并正常上传和清理。"""
    client = _build_feishu_client()
    uploaded = {}
    response = MagicMock(content=b"poster-bytes", headers={"Content-Type": "image/webp"})

    def upload_image(file_path: Path) -> FeishuResponse:
        """在临时文件删除前读取载荷，模拟飞书返回图片标识。"""
        uploaded["path"] = file_path
        uploaded["content"] = file_path.read_bytes()
        return FeishuResponse(code=0, msg="success", data={"image_key": "img_poster"})

    client._api_client.upload_image.side_effect = upload_image
    client._api_client.create_message.return_value = FeishuResponse(
        code=0, msg="success", data={"message_id": "om_test", "chat_id": "oc_test"},
    )
    with (
        patch("app.modules.feishu.feishu.RequestUtils") as request_utils,
        patch("app.modules.feishu.feishu.get_runtime_setting", side_effect={
            "USER_AGENT": "MoviePilot/Test", "PROXY": proxy,
        }.get),
        patch("app.adapters.network.ip.IpUtils.is_internal", return_value=internal),
    ):
        request_utils.return_value.get_res.return_value = response
        result = client.send_notification(
            Message(title="订阅完成", text="已入库", image=image_url), userid="ou_test",
        )

    assert result["success"]
    request_utils.assert_called_once()
    options = request_utils.call_args.kwargs
    assert options.get("referer") == expected_referer
    assert options.get("proxies") == expected_proxy
    assert options["ua"] == "MoviePilot/Test"
    assert options["timeout"] == 30
    request_utils.return_value.get_res.assert_called_once_with(image_url)
    response.close.assert_called_once()
    client._api_client.upload_image.assert_called_once()
    assert uploaded["content"] == b"poster-bytes"
    assert uploaded["path"].suffix == ".webp"
    assert not uploaded["path"].exists()
    content = json.loads(client._api_client.create_message.call_args.kwargs["content"])
    assert content["body"]["elements"][0]["img_key"] == "img_poster"
