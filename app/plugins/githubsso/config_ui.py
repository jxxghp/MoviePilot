"""GitHub 单点登录入口类型的配置契约与配置界面。

契约（``config_schema``）与界面（``config_form``）回答的不是同一个问题：契约是形状，
宿主据此在配置写入与实例构造两处拒绝畸形配置；界面是呈现，交给前端。两者并列声明而
不互相推导，因此这里各写一份。

契约描述的只是本类型自己的配置内容，即登录认证族配置模型 ``config`` 字段的形状。
``name``/``type``/``enabled``/``identity_provider`` 这四样属于服务族，由宿主持有，
契约不描述它们；其中 ``name`` 还会由宿主在构造实例时按关键字填入，契约再声明同名
字段会让构造得到两个 ``name``，宿主直接判整条声明不合契约。

凭据字段按键名被接口层识别并掩码下发：``client_secret`` 落在凭据键名表里，列表接口
一律换成掩码。``client_id`` 不在表里也不该在——OAuth 的 Client ID 本就要随授权跳转
出现在浏览器地址栏里，把它当秘密掩掉只会让用户核对不了自己填的是哪个 App。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from app.plugins.githubsso.entry import (
    DEFAULT_API_BASE_URL,
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
)

# 站点地址与 API 地址的取值形状：绝对的 http 或 https 地址
_HTTP_URL_PATTERN = r"^https?://[^\s]+$"

# 该类型配置内容的契约，取值落在 `app.runtime.extensions.contract.config_schema` 的受控子集内
CONFIG_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "title": "GitHub 单点登录",
    "description": "在 GitHub 上创建 OAuth App 后，把它的凭据与回调地址填在这里。",
    "properties": {
        "client_id": {
            "type": "string",
            "title": "Client ID",
            "description": "GitHub OAuth App 的 Client ID。",
            "minLength": 1,
            "maxLength": 255,
        },
        "client_secret": {
            "type": "string",
            "title": "Client Secret",
            "description": "GitHub OAuth App 的 Client Secret，列表接口一律以掩码下发。",
            "minLength": 1,
            "maxLength": 255,
        },
        "redirect_uri": {
            "type": "string",
            "title": "授权回调地址",
            "description": "须与 GitHub OAuth App 里登记的地址逐字一致，且指向本入口的回调路由。",
            "minLength": 1,
            "maxLength": 2048,
            "pattern": _HTTP_URL_PATTERN,
        },
        "base_url": {
            "type": "string",
            "title": "GitHub 站点地址",
            "description": "自建 GitHub Enterprise Server 填自己的地址。",
            "default": DEFAULT_BASE_URL,
            "minLength": 1,
            "maxLength": 2048,
            "pattern": _HTTP_URL_PATTERN,
        },
        "api_base_url": {
            "type": "string",
            "title": "GitHub API 地址",
            "description": "自建实例通常为站点地址加 /api/v3。",
            "default": DEFAULT_API_BASE_URL,
            "minLength": 1,
            "maxLength": 2048,
            "pattern": _HTTP_URL_PATTERN,
        },
        "allowed_organizations": {
            "type": "array",
            "title": "允许登录的组织",
            "description": "填了即只放行属于这些 GitHub 组织的账号，留空表示不限制。",
            "items": {"type": "string", "minLength": 1, "maxLength": 255},
            "maxItems": 64,
        },
        "request_timeout": {
            "type": "integer",
            "title": "请求超时（秒）",
            "description": "单次与 GitHub 往返的超时时间。",
            "default": DEFAULT_TIMEOUT_SECONDS,
            "minimum": MIN_TIMEOUT_SECONDS,
            "maximum": MAX_TIMEOUT_SECONDS,
        },
    },
    "required": ["client_id", "client_secret", "redirect_uri"],
    "additionalProperties": False,
}


def config_form() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """返回本类型的专属配置界面与它的默认数据。

    自带界面而不让前端按契约生成表单，是因为这几个字段单看字段名填不对：回调地址要与
    GitHub OAuth App 里登记的那一份逐字一致，组织名单填的是组织登录名而不是显示名，
    这些是提示文案才讲得清的事。

    界面里没有「设为默认」——登录认证族不设默认调用目标：族级默认回答的是「调用没指定
    用哪个」，而登录时用户点的是具体某个入口，不存在未指定这回事。

    :return: ``(组件树, 默认数据)`` 二元组
    """
    return [
        {
            "component": "VRow",
            "content": [
                _text_field("client_id", "Client ID", "GitHub OAuth App 的 Client ID"),
                _text_field(
                    "client_secret",
                    "Client Secret",
                    "GitHub OAuth App 的 Client Secret",
                    password=True,
                ),
            ],
        },
        {
            "component": "VRow",
            "content": [
                _text_field(
                    "redirect_uri",
                    "授权回调地址",
                    "须与 GitHub OAuth App 里登记的地址逐字一致",
                    cols=12,
                    md=12,
                ),
            ],
        },
        {
            "component": "VRow",
            "content": [
                _text_field("base_url", "GitHub 站点地址", "自建实例填自己的地址"),
                _text_field("api_base_url", "GitHub API 地址", "自建实例通常加 /api/v3"),
            ],
        },
        {
            "component": "VRow",
            "content": [
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 6},
                    "content": [
                        {
                            "component": "VCombobox",
                            "props": {
                                "model": "allowed_organizations",
                                "label": "允许登录的组织",
                                "hint": "填组织登录名，留空表示不限制",
                                "multiple": True,
                                "chips": True,
                                "clearable": True,
                                "persistent-hint": True,
                            },
                        }
                    ],
                },
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 6},
                    "content": [
                        {
                            "component": "VTextField",
                            "props": {
                                "model": "request_timeout",
                                "label": "请求超时（秒）",
                                "type": "number",
                                "hint": f"{MIN_TIMEOUT_SECONDS}–{MAX_TIMEOUT_SECONDS} 秒",
                                "persistent-hint": True,
                            },
                        }
                    ],
                },
            ],
        },
    ], {
        "client_id": "",
        "client_secret": "",
        "redirect_uri": "",
        "base_url": DEFAULT_BASE_URL,
        "api_base_url": DEFAULT_API_BASE_URL,
        "allowed_organizations": [],
        "request_timeout": DEFAULT_TIMEOUT_SECONDS,
    }


def _text_field(
    model: str,
    label: str,
    hint: str,
    *,
    password: bool = False,
    cols: int = 12,
    md: int = 6,
) -> Dict[str, Any]:
    """拼一列文本输入框。

    :param model: 绑定的配置字段名
    :param label: 字段标题
    :param hint: 字段提示文案
    :param password: 是否按密码框呈现
    :param cols: 窄屏下占的栅格数
    :param md: 中等及以上屏幕下占的栅格数
    :return: 一列 vuetify 组件描述
    """
    props: Dict[str, Any] = {
        "model": model,
        "label": label,
        "hint": hint,
        "persistent-hint": True,
    }
    if password:
        props["type"] = "password"
    return {
        "component": "VCol",
        "props": {"cols": cols, "md": md},
        "content": [{"component": "VTextField", "props": props}],
    }
