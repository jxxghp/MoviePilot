from unittest.mock import MagicMock, patch

from app.testing.bootstrap import ensure_optional_stub

ensure_optional_stub("psutil")
ensure_optional_stub("dateparser")
ensure_optional_stub("Pinyin2Hanzi", is_pinyin=lambda value: False)

from app.core.context import MediaInfo
from app.modules.feishu.feishu import Feishu
from app.schemas import Notification


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
            message=Notification(title="搜索结果", userid="ou_test"),
            medias=[first_media, second_media],
        )

    assert result == {"success": True}
    proxy_message = send_notification.call_args.args[0]
    assert proxy_message.image == "https://example.com/poster.jpg"
    assert proxy_message.text == "1. 无海报媒体\n2. 有海报媒体"
    assert send_notification.call_args.kwargs["userid"] == "ou_test"
