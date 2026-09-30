"""插件可依赖的飞书事件长连接接口，与宿主飞书模块共用同一传输实现。"""

from app.adapters.network.feishu import FEISHU_DOMAIN, FatalConnectionError, FeishuLongConnection

__all__ = ["FEISHU_DOMAIN", "FatalConnectionError", "FeishuLongConnection"]
