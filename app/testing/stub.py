"""
临时替换模块缓存并按快照恢复的测试工具。

stub_modules 在上下文中替换指定模块，snapshot_modules 和 restore_modules 可成对用于
测试夹具的准备与清理。所有操作只调整 sys.modules 映射，不会撤销导入期间的其他副作用，
也不会更新调用方已经绑定的对象引用或父包上的模块属性。
"""
import sys
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional


@contextmanager
def stub_modules(stubs: Dict[str, Any]) -> Iterator[None]:
    """
    在上下文中替换指定模块，退出时恢复原有模块对象。

    原条目缺失或值为 None 时，退出后移除该键；其他原值按对象恢复。
    仅恢复显式列出的模块，目标模块导入时产生的其他状态由调用方负责清理。

    :param stubs: 模块全名到占位对象的映射，通常使用 ModuleType 或 MagicMock
    :yields: 已替换模块的执行区间，不提供额外值
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
    """
    对指定范围内的模块缓存创建浅快照。

    快照不复制模块内部状态，也不冻结模块对象属性。

    :param prefix: 包名前缀；None 表示全部模块，末尾点号忽略，只匹配父包及其子模块
    :return: 模块全名到原对象的映射，可交给 restore_modules 恢复
    """
    if prefix is None:
        return dict(sys.modules)
    # 归一去掉末尾点后按"精确父模块或其子模块路径"匹配，避免 prefix="app" 误配到 "apple"
    parent = prefix.rstrip(".")
    return {k: v for k, v in sys.modules.items() if k == parent or k.startswith(parent + ".")}


def restore_modules(snapshot: Dict[str, Any], prefix: Optional[str] = None) -> None:
    """
    恢复快照范围内的模块映射，并移除该范围内后来新增的条目。

    仅恢复对象引用，不撤销模块内部属性变化或导入的其他副作用。

    :param snapshot: 此前 snapshot_modules 返回的浅快照
    :param prefix: 与取快照时一致的包名前缀；None 表示恢复全部模块
    """
    in_scope: Callable[[str], bool]
    if prefix is None:
        # 两个分支统一保存一个局部判定函数，保留赋值式 lambda 的既有写法。
        in_scope = lambda name: True  # noqa: E731
    else:
        head = prefix.rstrip(".")
        # 与快照使用相同的包边界；局部 lambda 统一后续遍历的范围判定。
        in_scope = lambda name: name == head or name.startswith(head + ".")  # noqa: E731
    # 移除范围内、快照中没有的新增项（通常是测试塞入的假桩）
    for name in [n for n in sys.modules if in_scope(n) and n not in snapshot]:
        sys.modules.pop(name, None)
    # 恢复范围内被替换/删除的项
    for name, module in snapshot.items():
        if in_scope(name) and sys.modules.get(name) is not module:
            sys.modules[name] = module
