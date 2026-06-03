"""测试辅助工具（主程序与插件仓共享）。

汇集主程序与插件仓共用的测试 harness，仅供测试使用、不参与运行时逻辑：

- :mod:`app.testing.stub`：测试期对 ``sys.modules`` 的临时打桩并自动还原，避免残留假模块相互污染；
- :mod:`app.testing.bootstrap`：隔离 CONFIG_DIR、建表、插件目录注入与 v1/v2 marker 等引导逻辑；
- :mod:`app.testing.network_guard`：autouse 拦截测试期对非本地主机的真实出站。

子模块各自按需 import（如 ``network_guard`` 依赖 pytest），故此处只 re-export 无第三方依赖的
:func:`stub_modules`，保持 ``import app.testing`` 不引入 pytest 等测试期依赖。
"""
from app.testing.stub import stub_modules

__all__ = ["stub_modules"]
