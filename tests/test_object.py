"""
验证反射工具对占位方法、装饰器与源码缓存的判断。
"""

import functools
import linecache

from app.foundation import reflection
from app.foundation.reflection import ObjectUtils


def test_check_method():
    """
    方法实现检测区分真实函数体与空实现、文档占位和未实现异常。
    """
    def implemented_function():
        return "Hello"

    def pass_function():
        pass

    def docstring_function():
        """
        只有文档而没有实现的方法占位。
        """

    def ellipsis_function():
        ...

    def not_implemented_function():
        raise NotImplementedError

    def not_implemented_function_with_call():
        raise NotImplementedError()

    async def multiple_lines_async_def(_param1: str,
                                       _param2: str):
        pass

    def empty_function():
        return

    assert ObjectUtils.check_method(implemented_function)
    assert not ObjectUtils.check_method(pass_function)
    assert not ObjectUtils.check_method(docstring_function)
    assert not ObjectUtils.check_method(ellipsis_function)
    assert not ObjectUtils.check_method(not_implemented_function)
    assert not ObjectUtils.check_method(not_implemented_function_with_call)
    assert not ObjectUtils.check_method(multiple_lines_async_def)
    assert ObjectUtils.check_method(empty_function)


class _Module:
    """
    模拟模块类：占位方法与已实现方法并存，并经装饰器包装。
    """

    def placeholder(self):
        """
        未实现
        """

    @staticmethod
    def _wrap(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        return wrapper

    @_wrap
    def decorated_placeholder(self):
        """
        保留无实现方法，用于验证装饰器展开后的判定。
        """
        pass

    def implemented(self):
        """
        返回固定结果，作为有实现方法的对照。
        """
        return 1


def test_check_method_handles_bound_and_decorated_methods():
    """
    方法实现检测兼容绑定方法与装饰器包裹的方法占位。
    """
    module = _Module()

    assert not ObjectUtils.check_method(module.placeholder)
    assert not ObjectUtils.check_method(module.decorated_placeholder)
    assert ObjectUtils.check_method(module.implemented)


def test_check_method_does_not_leave_source_in_linecache():
    """
    读取方法源码进行检测后，不在全局行缓存中残留该文件。
    """
    linecache.clearcache()

    def pass_function():
        pass

    assert not ObjectUtils.check_method(pass_function)
    assert __file__ not in linecache.cache


def test_check_method_caches_result_per_code_object(monkeypatch):
    """
    共享同一代码对象的闭包复用检测结果，避免重复读取源码。
    """
    calls = []
    original = reflection._read_function_source
    monkeypatch.setattr(
        reflection, "_read_function_source", lambda code: calls.append(code) or original(code)
    )

    def make():
        def pass_function():
            pass

        return pass_function

    # 两个闭包共享同一代码对象，只读取一次源码
    first, second = make(), make()
    assert not ObjectUtils.check_method(first)
    assert not ObjectUtils.check_method(second)
    assert calls == [first.__code__]
