"""sys.modules 临时打桩工具。

测试常需在 import 目标模块前，用假模块替换其依赖（避免连真实库 / 外部服务 / 重依赖）。
若打桩后不还原，假模块会残留在 ``sys.modules`` 中污染后续测试的 import。本模块提供
上下文管理器统一管理：进入时替换、退出时还原（原本存在的恢复、原本没有的删除）。
"""
import sys
from contextlib import contextmanager
from typing import Any, Dict, Iterator


@contextmanager
def stub_modules(stubs: Dict[str, Any]) -> Iterator[None]:
    """在上下文内用假模块临时替换 ``sys.modules`` 中的指定项，退出时还原。

    典型用法：在测试模块顶层包裹依赖打桩的 import，使打桩只在 import 期生效、
    随后立即还原，从而既满足导入需求又不污染其他测试。

    :param stubs: ``{模块全名: 假模块对象}``，假模块通常为 ``MagicMock()`` 或自建桩。

    用例::

        with stub_modules({"app.helper.sites": MagicMock()}):
            from app.chain.media import MediaChain
        # 此处 app.helper.sites 已还原为真实模块，MediaChain 已绑定可用
    """
    saved: Dict[str, Any] = {}
    for name, module in stubs.items():
        # 记录原值（含“原本不存在”用 None 标记），以便精确还原
        saved[name] = sys.modules.get(name)
        sys.modules[name] = module
    try:
        yield
    finally:
        for name, original in saved.items():
            if original is None:
                # 原本不存在则删除，避免遗留空桩
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
