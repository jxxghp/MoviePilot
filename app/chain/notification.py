from typing import Any, Dict, Tuple

from app.chain import ChainBase


class NotificationChain(ChainBase):
    """
    通知渠道管理链，仅做模块方法名契约的薄分发，渠道连接与能力全部封闭在模块内部
    """

    def get_wechatclawbot_status(
            self,
            source=None,
            fallback_source=None,
            WECHATCLAWBOT_BASE_URL=None,
            WECHATCLAWBOT_DEFAULT_TARGET=None,
            WECHATCLAWBOT_ADMINS=None,
            WECHATCLAWBOT_POLL_TIMEOUT=None,
            refresh_remote: bool = True,
            auto_generate_qrcode: bool = True,
    ) -> Dict[str, Any]:
        """查询微信 ClawBot 登录状态与二维码。"""
        result = self.run_module(
            "wechatclawbot_status",
            source=source,
            fallback_source=fallback_source,
            WECHATCLAWBOT_BASE_URL=WECHATCLAWBOT_BASE_URL,
            WECHATCLAWBOT_DEFAULT_TARGET=WECHATCLAWBOT_DEFAULT_TARGET,
            WECHATCLAWBOT_ADMINS=WECHATCLAWBOT_ADMINS,
            WECHATCLAWBOT_POLL_TIMEOUT=WECHATCLAWBOT_POLL_TIMEOUT,
            refresh_remote=refresh_remote,
            auto_generate_qrcode=auto_generate_qrcode,
        )
        return result or {"success": False, "message": "微信 ClawBot 通知未启用或配置尚未保存"}

    def refresh_wechatclawbot_qrcode(
            self,
            source=None,
            fallback_source=None,
            WECHATCLAWBOT_BASE_URL=None,
            WECHATCLAWBOT_DEFAULT_TARGET=None,
            WECHATCLAWBOT_ADMINS=None,
            WECHATCLAWBOT_POLL_TIMEOUT=None,
    ) -> Dict[str, Any]:
        """刷新微信 ClawBot 登录二维码。"""
        result = self.run_module(
            "wechatclawbot_refresh_qrcode",
            source=source,
            fallback_source=fallback_source,
            WECHATCLAWBOT_BASE_URL=WECHATCLAWBOT_BASE_URL,
            WECHATCLAWBOT_DEFAULT_TARGET=WECHATCLAWBOT_DEFAULT_TARGET,
            WECHATCLAWBOT_ADMINS=WECHATCLAWBOT_ADMINS,
            WECHATCLAWBOT_POLL_TIMEOUT=WECHATCLAWBOT_POLL_TIMEOUT,
        )
        return result or {"success": False, "message": "微信 ClawBot 通知未启用或配置尚未保存"}

    def logout_wechatclawbot(
            self,
            source=None,
            fallback_source=None,
            WECHATCLAWBOT_BASE_URL=None,
            WECHATCLAWBOT_DEFAULT_TARGET=None,
            WECHATCLAWBOT_ADMINS=None,
            WECHATCLAWBOT_POLL_TIMEOUT=None,
    ) -> Dict[str, Any]:
        """退出微信 ClawBot 登录。"""
        result = self.run_module(
            "wechatclawbot_logout",
            source=source,
            fallback_source=fallback_source,
            WECHATCLAWBOT_BASE_URL=WECHATCLAWBOT_BASE_URL,
            WECHATCLAWBOT_DEFAULT_TARGET=WECHATCLAWBOT_DEFAULT_TARGET,
            WECHATCLAWBOT_ADMINS=WECHATCLAWBOT_ADMINS,
            WECHATCLAWBOT_POLL_TIMEOUT=WECHATCLAWBOT_POLL_TIMEOUT,
        )
        return result or {"success": False, "message": "微信 ClawBot 通知未启用或配置尚未保存"}

    def test_wechatclawbot_connection(
            self,
            source=None,
            fallback_source=None,
            WECHATCLAWBOT_BASE_URL=None,
            WECHATCLAWBOT_DEFAULT_TARGET=None,
            WECHATCLAWBOT_ADMINS=None,
            WECHATCLAWBOT_POLL_TIMEOUT=None,
    ) -> Dict[str, Any]:
        """测试微信 ClawBot 当前登录态是否可用。"""
        result = self.run_module(
            "wechatclawbot_test_connection",
            source=source,
            fallback_source=fallback_source,
            WECHATCLAWBOT_BASE_URL=WECHATCLAWBOT_BASE_URL,
            WECHATCLAWBOT_DEFAULT_TARGET=WECHATCLAWBOT_DEFAULT_TARGET,
            WECHATCLAWBOT_ADMINS=WECHATCLAWBOT_ADMINS,
            WECHATCLAWBOT_POLL_TIMEOUT=WECHATCLAWBOT_POLL_TIMEOUT,
        )
        return result or {"success": False, "message": "微信 ClawBot 通知未启用或配置尚未保存"}

    def migrate_wechatclawbot_cache(
            self,
            old_name: str,
            new_name: str,
            cleanup_old: bool = False,
            overwrite: bool = False,
    ) -> Tuple[bool, str]:
        """在通知名称变更时迁移对应的微信 ClawBot 登录缓存。"""
        result = self.run_module(
            "wechatclawbot_migrate_cache",
            old_name=old_name,
            new_name=new_name,
            cleanup_old=cleanup_old,
            overwrite=overwrite,
        )
        return result or (False, "微信 ClawBot 通知未启用")
