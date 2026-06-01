"""测试辅助工具（主程序与插件仓共享）。

提供测试期对 ``sys.modules`` 的临时打桩能力，保证打桩在使用后还原，避免测试间
因残留假模块而相互污染。仅供测试使用，不参与运行时逻辑。
"""
from app.testing.stub import stub_modules

__all__ = ["stub_modules"]
