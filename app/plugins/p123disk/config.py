"""123 云盘存储类型的配置契约与配置界面。

契约（``config_schema``）与界面（``config_form``）回答的不是同一个问题：界面是呈现，
交给前端；契约是形状，宿主据此在「配置写入」与「构造实例」两处拒绝畸形配置并说明
原因。两者并列而不互相推导。

契约描述的只是本类型自己的配置内容，即存储配置模型 ``config`` 字段的形状；实例名、
类型标识、启用开关属于服务族的外壳，不由类型描述。

账号与密码都不列进 ``required``：用户在存储设置里先建实例、再填凭据是正常次序，也
可能把凭据清空后重填。凭据缺席时 `P123Storage.check` 返回 False，宿主照常把它显示成
一个尚未可用的实例，而不是让整条配置写不进去。

``password`` 会被接口层按键名识别为凭据并以掩码下发，前端原样回传掩码即表示未改动；
``passport`` 是账号本身而非凭据，不参与掩码，因此界面上照常显示原值。
"""

from typing import Any, Dict, List, Tuple

# 账号与密码的长度上限，与 123 云盘登录页一致的宽松取值
_PASSPORT_MAX_LENGTH = 64
_PASSWORD_MAX_LENGTH = 128

# 存储类型的配置契约，取值落在宿主支持的 JSON Schema 子集内
STORAGE_CONFIG_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "title": "123云盘账号",
    "description": "一份配置对应一个 123 云盘账号，账号之间互不共享空间与文件。",
    "properties": {
        "passport": {
            "type": "string",
            "title": "账号",
            "description": "登录 123 云盘的手机号或邮箱",
            "maxLength": _PASSPORT_MAX_LENGTH,
        },
        "password": {
            "type": "string",
            "title": "密码",
            "description": "登录密码，接口下发时以掩码代替，回传掩码表示不修改",
            "maxLength": _PASSWORD_MAX_LENGTH,
        },
    },
    "additionalProperties": False,
}


def storage_config_form() -> Tuple[List[dict], Dict[str, Any]]:
    """
    构造存储类型的专属配置界面

    :return: (组件树, 默认数据) 二元组，vuetify 模式
    """
    return [
        {
            "component": "VForm",
            "content": [
                {
                    "component": "VRow",
                    "content": [
                        _text_field("passport", "账号", "手机号或邮箱"),
                        _text_field("password", "密码", "登录密码", password=True),
                    ],
                }
            ],
        }
    ], {"passport": "", "password": ""}


def plugin_config_form() -> Tuple[List[dict], Dict[str, Any]]:
    """
    构造插件自身的配置界面

    界面上只有启用开关：账号属于存储实例，一个开关管不了两个账号，凭据因此落在
    存储实例的配置里而不是插件配置里。

    :return: (组件树, 默认数据) 二元组，vuetify 模式
    """
    return [
        {
            "component": "VForm",
            "content": [
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "enabled",
                                        "label": "启用插件",
                                    },
                                }
                            ],
                        }
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": "账号在「存储」设置里按实例填写，"
                                                "可添加多个 123 云盘账号。",
                                    },
                                }
                            ],
                        }
                    ],
                },
            ],
        }
    ], {"enabled": False}


def _text_field(model: str, label: str, hint: str, *, password: bool = False) -> dict:
    """
    构造一个占半行的文本输入框

    :param model: 绑定的配置字段名
    :param label: 字段标题
    :param hint: 字段提示
    :param password: 是否按密码框呈现
    :return: 组件描述
    """
    props: Dict[str, Any] = {"model": model, "label": label, "hint": hint}
    if password:
        props["type"] = "password"
    return {
        "component": "VCol",
        "props": {"cols": 12, "md": 6},
        "content": [{"component": "VTextField", "props": props}],
    }
