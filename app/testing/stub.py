"""sys.modules 临时打桩与快照还原工具。

测试常需在 import 目标模块前，用假模块替换其依赖（避免连真实库 / 外部服务 / 重依赖）。
若打桩后不还原，假模块会残留在 ``sys.modules`` 中污染后续测试的 import。本模块提供两类能力：

1. :func:`stub_modules` —— 上下文管理器，进入时替换、退出时精确还原；
2. :func:`snapshot_modules` / :func:`restore_modules` —— 快照与还原 ``sys.modules``，
   供测试在 setUp/tearDown 做整体自隔离，消除测试间通过 ``sys.modules`` 传播的污染。
"""
import sys
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional


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
        saved[name] = sys.modules.get(name)
        sys.modules[name] = module
    try:
        yield
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


def snapshot_modules(prefix: Optional[str] = None) -> Dict[str, Any]:
    """对当前 ``sys.modules`` 取浅快照，用于稍后还原。

    :param prefix: 仅快照名称匹配该前缀的模块（如 ``"app."``）；为 ``None`` 时快照全部。
                   还原以快照为准，能恢复被替换、删除的条目，并移除快照后新增的条目。
    :return: 快照字典（模块名 -> 模块对象），传给 :func:`restore_modules`。
    """
    if prefix is None:
        return dict(sys.modules)
    return {k: v for k, v in sys.modules.items() if k == prefix.rstrip(".") or k.startswith(prefix)}


def restore_modules(snapshot: Dict[str, Any], prefix: Optional[str] = None) -> None:
    """把 ``sys.modules`` 还原到 :func:`snapshot_modules` 的状态。

    被替换 / 删除的恢复为快照值；快照之后新增的（同前缀范围内）移除，避免假桩残留。

    :param snapshot: :func:`snapshot_modules` 返回的快照。
    :param prefix: 还原范围前缀；须与取快照时一致。为 ``None`` 时按全量还原。
    """
    if prefix is None:
        in_scope = lambda name: True  # noqa: E731
    else:
        head = prefix.rstrip(".")
        in_scope = lambda name: name == head or name.startswith(prefix)  # noqa: E731
    # 移除范围内、快照中没有的新增项（通常是测试塞入的假桩）
    for name in [n for n in sys.modules if in_scope(n) and n not in snapshot]:
        sys.modules.pop(name, None)
    # 恢复范围内被替换/删除的项
    for name, module in snapshot.items():
        if in_scope(name) and sys.modules.get(name) is not module:
            sys.modules[name] = module
