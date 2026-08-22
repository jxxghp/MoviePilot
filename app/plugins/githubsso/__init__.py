"""GitHub 单点登录：登录页上的 GitHub OAuth 入口。

本插件声明一个登录认证族的服务实例类型（``capability="auth"``）。用户在登录认证设置
里配几份，登录页上就有几个 GitHub 按钮；每份配置带自己的 OAuth 凭据、站点地址与组织
名单，彼此互不影响。

握手成功后不自行判定「这是哪个本项目用户」：账号对应关系落在宿主的第三方身份绑定表
里，由 ``create_plugin_auth_ticket_for_identity`` 按 ``(provider, external_id)`` 查
已绑定的用户并签发一次性登录票据。查不到时它返回 None，本插件照样不建号——首次第三方
登录默认不自动创建本项目用户，否则任何能登录该 GitHub 账号的人都能在本项目开号。
"""

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.plugins.githubsso.config_ui import CONFIG_SCHEMA, config_form
from app.plugins.githubsso.entry import GithubSsoAuthError, GithubSsoEntry
from app.plugins.githubsso.oauth_state import (
    STATE_TTL_SECONDS,
    OAuthStateStore,
    safe_return_path,
)
from app.sdk.auth import (
    AuthEntry,
    create_plugin_auth_ticket_for_identity,
    list_auth_entries,
)
from app.sdk.config import settings
from app.sdk.declarations import ServiceInstanceDeclaration
from app.sdk.extension import _PluginBase
from app.sdk.logging import logger
from app.sdk.services import declared_service_instances

# 本插件声明的服务族能力标签。取值须是宿主服务族登记表里的族，本次运行认哪些族由
# `app.sdk.service_instances` 的 ``service_capabilities()`` 回答
SERVICE_CAPABILITY = "auth"

# 本插件声明的登录入口类型标识，与用户配置里的 ``type`` 字段取值对应
SERVICE_TYPE = "github"

# 授权回调路由在本插件实例下的路径，回调地址须指向它
CALLBACK_PATH = "/callback"

# 一次性登录票据与失败原因回传给前端时所在的地址片段参数名。片段不随请求发往服务端，
# 因此票据不会落进服务端访问日志，也不会随 Referer 带去下一跳
TICKET_FRAGMENT_KEY = "mp_auth_ticket"
ERROR_FRAGMENT_KEY = "mp_auth_error"

# 承载浏览器绑定凭据的 Cookie 名。作用域收到回调路由这一条路径上，其余请求不带它
STATE_COOKIE_NAME = "githubsso_state"

# 回传给前端的失败原因取自本组固定文案，不回显请求带来的任何内容：回调是匿名开放的，
# 请求里的文字原样回显即等于让任何人往登录页的提示条里写字
REASON_AUTHORIZE_INCOMPLETE = "GitHub 授权未完成"
REASON_NO_CODE = "GitHub 未交回授权码"
REASON_HANDSHAKE_FAILED = "GitHub 登录出错，请稍后重试"
REASON_NOT_BOUND = "该 GitHub 账号尚未绑定本项目用户，请先登录后在设置页完成绑定"


class GithubSso(_PluginBase):
    """在登录页提供 GitHub OAuth 登录入口的插件。"""

    plugin_name = "GitHub 单点登录"
    plugin_desc = "在登录页提供 GitHub OAuth 登录入口，按第三方身份绑定登录已绑定的用户。"
    plugin_icon = "github.png"
    plugin_version = "1.0"
    plugin_author = "MoviePilot"
    author_url = "https://github.com/jxxghp/MoviePilot"
    plugin_config_prefix = "githubsso_"
    plugin_order = 30
    auth_level = 1

    def __init__(self, plugin_id: Optional[str] = None, instance_id: Optional[str] = None):
        """建立本实例的启用态与授权往返状态存储。

        :param plugin_id: 插件标识
        :param instance_id: 实例标识
        """
        super().__init__(plugin_id=plugin_id, instance_id=instance_id)
        self._enabled = False
        self._states = OAuthStateStore()

    def init_plugin(self, config: dict = None):
        """生效插件配置。

        :param config: 插件自身的配置，登录入口的凭据不在这里，而在登录认证族的
            实例配置里
        """
        self._enabled = bool((config or {}).get("enabled"))

    def get_state(self) -> bool:
        """返回插件启用状态。

        :return: 插件是否已启用
        """
        return self._enabled

    def stop_service(self):
        """停止插件，丢弃尚未完成的授权往返上下文。"""
        self._states.clear()

    @staticmethod
    def get_render_mode() -> Tuple[str, Optional[str]]:
        """返回本插件的渲染模式。

        :return: ``(渲染模式, 联邦构建产物路径)``
        """
        return "vuetify", None

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """拼装插件自身的配置界面。

        插件本体只有一个启用开关：登录入口的凭据属于登录认证族的实例配置，界面由
        `provides_service_instances()` 那条声明的 ``config_form`` 承担，不放在这里。

        :return: ``(组件树, 默认数据)`` 二元组
        """
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "enabled",
                                    "label": "启用插件",
                                    "hint": "关闭后登录页不再显示本插件提供的入口",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    }
                ],
            }
        ], {"enabled": False}

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。

        :return: 页面配置；本插件没有详情页，返回 None
        """
        return None

    def get_api(self) -> List[Dict[str, Any]]:
        """注册本插件的 HTTP 接口。

        两个接口都匿名开放：它们是登录流程本身，请求发生在任何用户会话之前，要求
        鉴权等于要求用户先登录才能登录。匿名不等于无凭据——发起授权只吐出一个由服务端
        生成的跳转地址，回调则必须带回服务端签发且尚未用过的 state 才会继续。

        :return: 接口描述列表
        """
        return [
            {
                "path": "/authorize",
                "endpoint": self.authorize,
                "methods": ["GET"],
                "allow_anonymous": True,
                "summary": "发起 GitHub 单点登录",
                "description": "按登录入口标识生成 GitHub 授权跳转地址",
            },
            {
                "path": CALLBACK_PATH,
                "endpoint": self.callback,
                "methods": ["GET"],
                "allow_anonymous": True,
                "summary": "GitHub 单点登录回调",
                "description": "校验 state、兑换授权码并签发一次性登录票据",
            },
        ]

    def provides_service_instances(self) -> Optional[List[ServiceInstanceDeclaration]]:
        """声明本插件提供的登录入口类型。

        ``multi_instance=True``：本类型的站点地址可配，因此第二份配置指的是另一个
        GitHub 部署——自建的 GitHub Enterprise Server 与 github.com 是两套互不相干的
        账号命名空间，同一个数字账号标识在两边指的是两个人。这与「同一个服务配两份」
        不是一回事，后者才该声明单实例。

        同一个部署上配两份（例如两个各自限定组织的 OAuth App）也成立，但要知道代价：
        两条配置各自派生出 ``github@实例名`` 这一个身份绑定标识，同一个 GitHub 账号
        经两个入口登录会落成两条互不相干的绑定。想让它们共用一个身份空间是做不到的——
        两条配置填同一个身份绑定标识即身份歧义，宿主会让两条都不产出入口。

        :return: 服务实例类型声明列表
        """
        return [
            ServiceInstanceDeclaration(
                capability=SERVICE_CAPABILITY,
                type=SERVICE_TYPE,
                name="GitHub 单点登录",
                icon="mdi-github",
                impl=GithubSsoEntry,
                multi_instance=True,
                config_form=config_form(),
                config_schema=CONFIG_SCHEMA,
            )
        ]

    def authorize(self, provider: str, return_to: str = "") -> JSONResponse:
        """发起一次 GitHub 授权，交回浏览器应当前往的授权地址。

        授权地址里带 state，响应的 Cookie 里带配套的浏览器绑定凭据：前者随地址走遍
        整条回调链路，后者只留在发起授权的这个浏览器里，回调时两样都要对得上。

        :param provider: 登录入口标识，取自登录页入口列表里的 ``id``
        :param return_to: 登录成功后回跳的站内相对路径，不合形状时回落到站点根路径
        :return: 含 ``authorize_url`` 的响应，并在 Cookie 里带上浏览器绑定凭据
        :raises HTTPException: 入口不存在、未启用或其配置不可用
        """
        entry, instance = self._resolve_entry(provider)
        callback_path = self._callback_path()
        if instance.callback_path != callback_path:
            logger.error(
                f"登录入口 {entry.identity} 配置的授权回调地址指向 "
                f"{instance.callback_path}，与本入口的回调路由 {callback_path} 不一致"
            )
            raise HTTPException(status_code=400, detail="登录入口的授权回调地址配置有误")
        state, binding = self._states.issue(entry.identity, safe_return_path(return_to))
        response = JSONResponse({"authorize_url": instance.authorize_url(state)})
        response.set_cookie(
            key=STATE_COOKIE_NAME,
            value=binding,
            max_age=STATE_TTL_SECONDS,
            path=callback_path,
            httponly=True,
            # 回调是 GitHub 发起的顶层跳转，Lax 照常带上本 Cookie，跨站的子请求则不带
            samesite="lax",
            secure=instance.redirect_uri.startswith("https://"),
        )
        return response

    def callback(
        self,
        request: Request,
        code: str = "",
        state: str = "",
        error: str = "",
        error_description: str = "",
    ) -> Response:
        """承接 GitHub 的授权回调，签发一次性登录票据。

        state 与浏览器绑定凭据先核对再做别的：任何一样对不上，这次回调就不对应本浏览器
        任何一次尚未完成的授权，此后所有取值都不可信，包括回跳地址。核对通过之后失败的
        每一步都回跳到发起授权时收敛过的那个站内路径。

        回跳片段里的失败原因取自本模块的固定文案。GitHub 报回的错误描述只进日志不回显：
        本接口匿名开放，谁都能构造一次带自己 state 的回调，回显请求里的文字等于把登录页
        的提示条开放给任何人书写。

        :param request: 本次回调请求，用于读取浏览器绑定凭据
        :param code: GitHub 交回的授权码
        :param state: 发起授权时服务端签发的 state
        :param error: GitHub 报回的错误标识，用户拒绝授权时出现
        :param error_description: GitHub 报回的错误描述，只进日志
        :return: 回跳到站内路径的重定向响应，片段里带一次性登录票据或失败原因
        :raises HTTPException: state 缺失、不存在、已过期，或与本浏览器对不上
        """
        record = self._states.consume(state)
        if record is None or not record.matches_binding(
            request.cookies.get(STATE_COOKIE_NAME)
        ):
            raise HTTPException(status_code=400, detail="授权请求已失效，请重新发起登录")
        if error:
            logger.warning(
                f"登录入口 {record.identity} 的 GitHub 授权未完成：{error} {error_description}"
            )
            return self._redirect_back(record.return_path, REASON_AUTHORIZE_INCOMPLETE)
        if not code:
            return self._redirect_back(record.return_path, REASON_NO_CODE)
        try:
            ticket = self._issue_ticket(record.identity, code)
        except HTTPException as failure:
            return self._redirect_back(record.return_path, str(failure.detail))
        except GithubSsoAuthError as failure:
            logger.warning(f"登录入口 {record.identity} 的 GitHub 握手失败：{failure}")
            return self._redirect_back(record.return_path, REASON_HANDSHAKE_FAILED)
        except Exception as failure:
            logger.error(f"登录入口 {record.identity} 的 GitHub 登录出错：{failure}")
            return self._redirect_back(record.return_path, REASON_HANDSHAKE_FAILED)
        if not ticket:
            return self._redirect_back(record.return_path, REASON_NOT_BOUND)
        return self._finish(
            RedirectResponse(
                url=f"{record.return_path}#{TICKET_FRAGMENT_KEY}={quote(ticket)}",
                status_code=303,
            )
        )

    def _issue_ticket(self, identity: str, code: str) -> Optional[str]:
        """完成一次握手并向宿主换取一次性登录票据。

        ``provider_id`` 原样回传登录入口标识：它是身份绑定表 ``provider`` 列的取值，
        由宿主按入口配置裁决出来，插件自行拼一个就是换了个身份命名空间。``external_id``
        取 GitHub 账号的数字标识而不是登录名，登录名改得掉，改完原名可被他人注册走。

        :param identity: 登录入口标识
        :param code: GitHub 交回的授权码
        :return: 一次性登录票据；该身份尚未绑定本项目用户时为 None
        :raises HTTPException: 入口不可用，或该账号不在允许登录的组织名单内
        :raises GithubSsoAuthError: 与 GitHub 的往返失败
        """
        _, instance = self._resolve_entry(identity)
        access_token = instance.exchange_code(code)
        allowed, reason = instance.organization_allowed(access_token)
        if not allowed:
            raise HTTPException(status_code=403, detail=reason)
        github = instance.fetch_identity(access_token)
        return create_plugin_auth_ticket_for_identity(
            provider_id=identity,
            external_id=github.external_id,
            display_name=github.display_name,
            metadata={"service_type": SERVICE_TYPE, "login": github.login},
        )

    def _resolve_entry(self, identity: str) -> Tuple[AuthEntry, GithubSsoEntry]:
        """按登录入口标识定位本实例名下的那条入口与它的握手实现。

        入口从宿主的登录入口列表里取而不是自行按配置推导：那份列表已经做完启用态筛选、
        单实例裁决与身份标识去歧义，被去歧义剔除掉的入口在登录页上是不存在的，插件自行
        推导就会替一个用户看不见的入口完成认证。

        实例从宿主的服务实例登记表里取而不是自行构造：构造形状与配置契约判定都由宿主
        那一份实现负责，插件自己构造既会绕开契约判定，也会在构造协议变化时失配。

        :param identity: 登录入口标识
        :return: ``(登录入口, 握手实现)``
        :raises HTTPException: 入口不存在、不归本实例所有，或其配置构造不出实例
        """
        owner = self.get_instance_key()
        matched = [
            entry
            for entry in list_auth_entries()
            if entry.identity == identity
            and entry.owner == owner
            and entry.service_type == SERVICE_TYPE
        ]
        if len(matched) != 1:
            logger.warning(
                f"插件实例 {owner} 名下命中 {len(matched)} 条标识为 {identity} 的登录入口，"
                f"本次登录不予继续"
            )
            raise HTTPException(status_code=404, detail="登录入口不存在或未启用")
        entry = matched[0]
        instance = self._registered_instance(owner, entry.name)
        if instance is None:
            raise HTTPException(status_code=400, detail="登录入口的配置不可用")
        return entry, instance

    def _registered_instance(self, owner: str, name: str) -> Optional[GithubSsoEntry]:
        """从服务实例登记表里取本实例名下某个具名实例。

        :param owner: 本插件实例的实例键
        :param name: 实例名
        :return: 握手实现；登记缺失或该条配置构造失败时为 None
        """
        instance = declared_service_instances(
            SERVICE_CAPABILITY, SERVICE_TYPE, owner
        ).get(name)
        return instance if isinstance(instance, GithubSsoEntry) else None

    def _callback_path(self) -> str:
        """返回本插件实例回调路由的完整路径。

        :return: 回调路由路径
        """
        return f"{settings.API_V1_STR}/plugin/{self.get_instance_key()}{CALLBACK_PATH}"

    def _redirect_back(self, return_path: str, reason: str) -> RedirectResponse:
        """带着失败原因回跳到发起登录的页面。

        :param return_path: 发起授权时留存的站内相对路径
        :param reason: 面向用户的失败原因，取自本模块的固定文案
        :return: 重定向响应
        """
        return self._finish(
            RedirectResponse(
                url=f"{return_path}#{ERROR_FRAGMENT_KEY}={quote(reason)}", status_code=303
            )
        )

    def _finish(self, response: RedirectResponse) -> RedirectResponse:
        """收尾一次授权往返，撤掉这次往返的浏览器绑定凭据。

        无论成败都撤：凭据的用处到回调核对为止，留在浏览器里只会等着被下一次往返
        的核对撞上。

        :param response: 即将交出去的重定向响应
        :return: 同一个响应
        """
        response.delete_cookie(key=STATE_COOKIE_NAME, path=self._callback_path())
        return response
