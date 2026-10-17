"""
主程序与插件仓共享的测试隔离工具入口。

bootstrap 负责配置目录隔离、测试数据库装配和插件代际标记；network 提供自动启用的
网络守卫；stub 提供临时模块替换和恢复工具。这里只导出没有第三方依赖的 stub_modules，
避免导入测试工具包时提前加载应用配置或 pytest。
"""
from app.testing.stub import stub_modules

__all__ = ["stub_modules"]
