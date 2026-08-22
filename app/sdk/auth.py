"""登录认证族扩展要读的入口列表与要调的票据签发。

``list_auth_entries()`` 承载正确性而不只是便利：它已做完启用态筛选、单实例裁决与身份
标识去歧义。扩展若自行按配置推导 ``类型@实例名``，就会替一个被去歧义剔除、登录页上根本
不存在的入口完成认证——那是把另一台服务器的账号并进本入口的身份命名空间。

``create_plugin_auth_ticket_for_identity()`` 是握手成功后换取一次性登录票据的唯一入口。
``provider_id`` 要原样回传 ``AuthEntry.identity``：它是第三方身份绑定唯一键的一半，扩展
自行拼一个即换了一个身份命名空间，存量绑定全部落空。
"""

from app.application.security.auth import create_plugin_auth_ticket_for_identity
from app.runtime.extensions.projection.auth_entries import AuthEntry, list_auth_entries


__all__ = ["AuthEntry", "create_plugin_auth_ticket_for_identity", "list_auth_entries"]
