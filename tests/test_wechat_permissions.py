"""
验证企业微信菜单和文本命令的管理员权限边界。
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.modules.wechat import WechatModule


def _parse_encrypted_xml(xml_message: bytes, config: dict, client: SimpleNamespace):
    """
    使用模拟解密结果解析企业微信自建应用回调。
    """
    module = WechatModule()
    crypt = Mock()
    crypt.DecryptMsg.return_value = (0, xml_message)

    with (
        patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(name="wechat-test", config=config),
        ),
        patch.object(module, "get_instance", return_value=client),
        patch(
            "app.modules.wechat.WXBizMsgCrypt",
            return_value=crypt,
        ),
    ):
        return module.message_parser(
            source="wechat-test",
            body=b"encrypted",
            form={},
            args={"msg_signature": "sig", "timestamp": "1", "nonce": "n"},
        )


def test_menu_click_blocks_non_admin() -> None:
    """
    非管理员点击企业微信菜单时应被拦截。
    """
    client = SimpleNamespace(send_msg=Mock())
    message = _parse_encrypted_xml(
        b"""
            <xml>
              <FromUserName><![CDATA[user-2]]></FromUserName>
              <MsgType><![CDATA[event]]></MsgType>
              <Event><![CDATA[click]]></Event>
              <EventKey><![CDATA[/sites]]></EventKey>
            </xml>
            """,
        {
            "WECHAT_TOKEN": "token",
            "WECHAT_ENCODING_AESKEY": "encoding",
            "WECHAT_CORPID": "corpid",
            "WECHAT_ADMINS": "user-1",
        },
        client,
    )

    assert message is None
    client.send_msg.assert_called_once_with(title="只有管理员才有权限执行此命令", userid="user-2")


def test_menu_click_allows_admin_with_padded_config() -> None:
    """
    管理员配置含空格时，菜单权限判断仍应正确放行。
    """
    client = SimpleNamespace(send_msg=Mock())
    message = _parse_encrypted_xml(
        b"""
            <xml>
              <FromUserName><![CDATA[user-1]]></FromUserName>
              <MsgType><![CDATA[event]]></MsgType>
              <Event><![CDATA[click]]></Event>
              <EventKey><![CDATA[/sites]]></EventKey>
            </xml>
            """,
        {
            "WECHAT_TOKEN": "token",
            "WECHAT_ENCODING_AESKEY": "encoding",
            "WECHAT_CORPID": "corpid",
            "WECHAT_ADMINS": " admin-1, user-1 ",
        },
        client,
    )

    assert message is not None
    assert message.text == "/sites"
    client.send_msg.assert_not_called()


def test_text_command_blocks_non_admin() -> None:
    """
    非管理员发送企业微信斜杠命令时应被拦截。
    """
    client = SimpleNamespace(send_msg=Mock())
    message = _parse_encrypted_xml(
        b"""
            <xml>
              <FromUserName><![CDATA[user-2]]></FromUserName>
              <MsgType><![CDATA[text]]></MsgType>
              <Content><![CDATA[/sites]]></Content>
            </xml>
            """,
        {
            "WECHAT_TOKEN": "token",
            "WECHAT_ENCODING_AESKEY": "encoding",
            "WECHAT_CORPID": "corpid",
            "WECHAT_ADMINS": "user-1",
        },
        client,
    )

    assert message is None
    client.send_msg.assert_called_once_with(title="只有管理员才有权限执行此命令", userid="user-2")


def test_bot_text_command_blocks_non_admin() -> None:
    """
    企业微信智能机器人模式也应拦截非管理员斜杠命令。
    """
    module = WechatModule()
    client = SimpleNamespace(send_msg=Mock())
    body = json.dumps(
        {
            "body": {
                "from": {"userid": "user-2"},
                "msgtype": "text",
                "text": {"content": "/sites"},
            }
        }
    )

    with (
        patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(
                name="wechat-bot-test",
                config={"WECHAT_MODE": "bot", "WECHAT_ADMINS": "user-1"},
            ),
        ),
        patch.object(module, "get_instance", return_value=client),
    ):
        message = module.message_parser(
            source="wechat-bot-test",
            body=body,
            form={},
            args={},
        )

    assert message is None
    client.send_msg.assert_called_once_with(title="只有管理员才有权限执行此命令", userid="user-2")
