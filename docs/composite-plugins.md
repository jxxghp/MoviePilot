# 复合插件：一个插件一键拓展多个同类能力

MoviePilot 的插件声明式注册（`provides_*` 钩子）天然支持**一个插件用同一注册器注册多个同类项**，
无需任何额外开关。这让你能做"复合插件 / 一键式拓展"：一次安装即拉起一整套同类能力。

举例：
- 消息通知：`Wechat + Telegram + 邮件` 一键拓展
- 认证 SSO：`GitHub + Google + Microsoft` 一键拓展
- 下载器：`Aria2 + 迅雷 + P2P` 一键拓展
- 元数据：`IMDb + TVDB + 网易爆米花` 一键拓展
- 推荐/发现源：`爱奇艺 + 腾讯 + 芒果 + 优酷` 一键拓展

## 原理

每个 `provides_*` 钩子返回的都是 **List**；框架聚合器按 `plugin_id`(owner) 归集成 `{plugin_id: [items]}`，
逐项注册到对应注册器。所有下游注册器都是 **owner-scoped**：按各自 id 单索引存储、带 owner 记账、
按 owner **批量卸载**。因此同一插件注册多个不同 id 的项天然成立，停用插件时一次性全清。

| 能力 | 钩子 | 返回 | 注册去向 |
|---|---|---|---|
| 消息渠道 | `provides_notifications()` | 渠道模块类列表 | `ModuleManager` + 各模块自带 `get_channel_capabilities()` 登记到 `ChannelCapabilityManager` |
| SSO 登录 | `provides_auth_providers()` | `IAuthProvider` 实例列表 | `app.core.auth.redirect` 提供方注册表 |
| 认证流程/步骤 | `provides_auth_flows()` / `provides_auth_steps()` | 实例列表 | `flow_registry` / `steps` 注册表 |
| 下载器 | `provides_downloaders()` | 下载器模块类列表 | `ModuleManager`（Downloader 域） |
| 元数据/识别源 | `provides_data_sources()` | 模块类列表 | `ModuleManager`（MediaRecognize 域） |
| 媒体服务器 | `provides_mediaservers()` | 模块类列表 | `ModuleManager`（MediaServer 域） |
| 存储器 | `provides_storages()` | `StorageBase` 子类列表 | `FileManager` 存储注册表 |
| 发现/推荐源 | `provides_discover_sources()` / `provides_recommend_sources()` | 数据对象列表 | `/api/discover/source`、`/api/recommend/source` 端点聚合 |

## 两条铁律

1. **每个子项 id 必须唯一**，且不得与内建或其它插件相撞，否则后注册者被拒：
   - 模块类（通知/下载器/元数据/媒体服务器）：靠**类名** + `get_subtype_id()` 区分；
   - SSO 提供方 / 流程 / 步骤：靠 `provider_id` / `flow_id` / `step_id`（`provider_id` 须为 1–32 位字母数字连字符）；
   - 存储器：靠 `schema.value`；
   - 发现/推荐源：靠 `api_path`（端点按 api_path 去重）。
2. **生命周期是插件整体**：启用/停用插件 = 一次性注册/卸载它声明的全部子项。框架不提供"单独关闭某个
   子项"——如需子项级开关，请在插件自身配置里实现，并由各模块的 `init_setting()` 开关或子项的
   `applies_to()` 自行裁剪。

## 完整示例：一个一键拓展三路通知 + 三路 SSO 的复合插件

```python
from app.plugins import _PluginBase
from app.modules import _ModuleBase
from app.schemas.message import ChannelCapabilities, ChannelCapability
from app.schemas.types import ModuleType


# —— 三个消息渠道模块（各自带能力矩阵）——
class _WechatLikeChannel(_ModuleBase):
    def init_module(self): ...
    def init_setting(self): return None
    def stop(self): ...
    def test(self): return True, ""
    @staticmethod
    def get_type(): return ModuleType.Notification
    def get_subtype_id(self): return "mycombo-wechat"        # ← channel id 唯一声明处
    def post_message(self, message, **kwargs): ...           # 发送实现
    def get_channel_capabilities(self):
        return ChannelCapabilities(channel="mycombo-wechat",
                                   capabilities={ChannelCapability.MARKDOWN, ChannelCapability.IMAGES})


class _TelegramLikeChannel(_WechatLikeChannel):
    def get_subtype_id(self): return "mycombo-telegram"
    def get_channel_capabilities(self):
        return ChannelCapabilities(channel="mycombo-telegram",
                                   capabilities={ChannelCapability.INLINE_BUTTONS, ChannelCapability.MARKDOWN})


class _EmailChannel(_WechatLikeChannel):
    def get_subtype_id(self): return "mycombo-email"
    def get_channel_capabilities(self):
        return ChannelCapabilities(channel="mycombo-email", capabilities={ChannelCapability.LINKS})


# —— 三个 SSO 登录提供方 ——
class _SSOProvider:
    def __init__(self, pid, name):
        self.provider_id, self.provider_name, self.provider_icon = pid, name, f"mdi-{pid}"
    def authorize_url(self, state, redirect_uri): ...        # 构造 IdP 授权 URL
    def fetch_identity(self, code, redirect_uri): ...        # 授权码换身份


class ComboPlugin(_PluginBase):
    plugin_name = "一键多渠道 + 多 SSO"

    def get_state(self) -> bool:
        return True  # 实际应读插件自身的启用配置

    # 一次声明三个消息渠道
    def provides_notifications(self):
        return [_WechatLikeChannel, _TelegramLikeChannel, _EmailChannel]

    # 一次声明三个 SSO 提供方（id 唯一、字母数字连字符）
    def provides_auth_providers(self):
        return [_SSOProvider("github", "GitHub"),
                _SSOProvider("google", "Google"),
                _SSOProvider("microsoft", "Microsoft")]
```

安装并启用 `ComboPlugin` 后，三个消息渠道（含能力矩阵）与三个 SSO 入口一并上线；停用插件时全部一起卸载。
下载器 / 元数据 / 发现推荐源同理——把对应 `provides_*` 钩子返回多项即可。

回归测试见 `tests/test_composite_plugin_registration.py`。
